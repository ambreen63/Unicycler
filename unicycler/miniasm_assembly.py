"""
Copyright 2017 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Unicycler

This module contains functionality related to miniasm, which Unicycler uses to build an assembly
using both Illumina contigs and long reads.

This file is part of Unicycler. Unicycler is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Unicycler is distributed in
the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Unicycler. If
not, see <http://www.gnu.org/licenses/>.
"""

import os
import shutil
import statistics
from collections import defaultdict
from .misc import green, red
from .minimap_alignment import align_long_reads_to_assembly_graph, build_start_end_overlap_sets
from .cpp_wrappers import minimap_align_reads, miniasm_assembly
from .string_graph import StringGraph
from . import log
from . import settings


class MiniasmFailure(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return repr(self.message)


def build_miniasm_bridges(graph, out_dir, keep, threads, read_dict, long_read_filename,
                          scoring_scheme):
    """
    EXTRACT READS USEFUL FOR LONG READ ASSEMBLY.
    * Take all single copy contigs over a certain length and get reads which overlap two or more.
      * While I'm at it, I should throw out reads which look like chimeras based on incompatible
        mapping.
    * Create a file of "long reads" which contains:
      * real long reads as found (and possibly split) by the above step
      * single copy contigs in FASTQ form (with a high quality, 'I' or something)

    """
    log.log_section_header('Assemble contigs and long reads with miniasm and Racon')
    log.log_explanation('Unicycler uses miniasm to construct a string graph '
                        'assembly using both the short read contigs and the long reads. If this '
                        'produces an assembly, Unicycler will extract bridges between '
                        'contigs, improve them with Racon and use them to simplify the assembly '
                        'graph. This method requires decent coverage of long reads and therefore '
                        'may not be fruitful if long reads are sparse. However, this method does '
                        'not rely on the short read assembly graph having good connectivity and is '
                        'able to bridge an assembly graph even when it contains many dead ends.',
                        extra_empty_lines_after=0)
    log.log_explanation('Unicycler uses two types of "reads" as assembly input: '
                        'sufficiently long single-copy short read contigs and actual long reads '
                        'which overlap two or more of these contigs. It then assembles them with '
                        'a modified version of miniasm which gives precedence to the contigs over '
                        'the real long reads.', extra_empty_lines_after=0)
    log.log_explanation('Miniasm removes sequences which are contained in another sequence, and '
                        'this can result in short read contigs being lost in the string graph '
                        '(particularly common if the long reads are very long). If this happens, '
                        'the reads will be split in where they contain a contig and Unicycler '
                        'will repeat the miniasm assembly until no contigs are lost.')

    miniasm_dir = os.path.join(out_dir, 'miniasm_assembly')
    if not os.path.exists(miniasm_dir):
        os.makedirs(miniasm_dir)

    # Align all the long reads to the graph and get the ones which overlap single-copy contigs
    # (and will therefore be useful for assembly).
    minimap_alignments = align_long_reads_to_assembly_graph(graph, long_read_filename,
                                                            miniasm_dir, threads)
    start_overlap_reads, end_overlap_reads = build_start_end_overlap_sets(minimap_alignments)
    assembly_read_names = get_miniasm_assembly_reads(minimap_alignments, graph)

    assembly_reads_filename = os.path.join(miniasm_dir, '01_assembly_reads.fastq')
    mappings_filename = os.path.join(miniasm_dir, '02_mappings.paf')
    before_transitive_reduction_filename = os.path.join(miniasm_dir, '03_raw_string_graph.gfa')
    string_graph_filename = os.path.join(miniasm_dir, '10_final_string_graph.gfa')
    miniasm_output_filename = os.path.join(miniasm_dir, 'miniasm.out')

    # The miniasm assembly may be done multiple times. It reports whether a contig was contained in
    # a read, and if so, we will split the read and try again.
    read_break_points = defaultdict(set)
    while True:
        mean_read_quals = save_assembly_reads_to_file(assembly_reads_filename, assembly_read_names,
                                                      read_dict, graph, read_break_points)

        # Do an all-vs-all alignment of the assembly FASTQ, for miniasm input.
        minimap_alignments_str = minimap_align_reads(assembly_reads_filename, assembly_reads_filename,
                                                     threads, 0, 'read vs read')
        with open(mappings_filename, 'wt') as mappings:
            mappings.write(minimap_alignments_str)

        # Now actually do the miniasm assembly, which will create a GFA file of the string graph.
        # TO DO: intelligently set the min_ovlp setting (currently 1) based on the depth? The
        #        miniasm default is 3, so perhaps use 3 if the depth is high enough, 2 if it's
        #        lower and 1 if it's very low. I'm not yet sure what the risks are (if any) with
        #        using a min_ovlp of 1 when the depth is high.
        log.log('Assembling reads with miniasm... ', end='')
        miniasm_assembly(assembly_reads_filename, mappings_filename, miniasm_dir)

        contained_contig_count = 0
        with open(miniasm_output_filename, 'rt') as miniasm_out:
            for line in miniasm_out:
                line = line.strip()
                if line.startswith('CONTAINED CONTIG'):
                    line_parts = line.split('\t')
                    read_name = line_parts[2]
                    read_offset = 0
                    if '_range_' in read_name:
                        name_parts = read_name.split('_range_')
                        assert len(name_parts) == 2
                        read_name = name_parts[0]
                        read_offset = int(name_parts[1].split('-')[0])
                    read_start, read_end = int(line_parts[3]), int(line_parts[4])
                    read_break_points[read_name].add(read_offset + ((read_start + read_end) // 2))
                    contained_contig_count += 1

        # If the assembly finished without any contained contigs, then we're good to continue!
        if contained_contig_count == 0:
            break
        else:
            log.log(red('contigs lost in miniasm assembly'))
            log.log('Breaking reads and trying again\n')

            print(read_break_points)  # TEMP

    if not (os.path.isfile(string_graph_filename) and
            os.path.isfile(before_transitive_reduction_filename)):
        log.log(red('failed'))
        raise MiniasmFailure('miniasm failed to generate a string graph')
    string_graph = StringGraph(string_graph_filename, mean_read_quals)
    before_transitive_reduction = StringGraph(before_transitive_reduction_filename, mean_read_quals)

    log.log(green('success'))
    log.log('  ' + str(len(string_graph.segments)) + ' segments, ' +
            str(len(string_graph.links) // 2) + ' links', verbosity=2)

    string_graph.remove_branching_paths()
    if keep >= 3:
        string_graph.save_to_gfa(os.path.join(miniasm_dir, '12_branching_paths_removed.gfa'))

    string_graph.simplify_bridges(before_transitive_reduction)
    if keep >= 3:
        string_graph.save_to_gfa(os.path.join(miniasm_dir, '13_simplified_bridges.gfa'))

    string_graph.remove_overlaps(before_transitive_reduction, scoring_scheme)
    if keep >= 3:
        string_graph.save_to_gfa(os.path.join(miniasm_dir, '14_overlaps_removed.gfa'))

    string_graph.merge_reads()
    if keep >= 3:
        string_graph.save_to_gfa(os.path.join(miniasm_dir, '15_reads_merged.gfa'))

    # Some single copy contigs might be isolated from the main part of the graph (due to contained
    # read filtered or some graph simplification step, like bubble popping). We now need to place
    # them back in by aligning to the non-contig graph segments.
    string_graph.place_isolated_contigs(miniasm_dir, threads, before_transitive_reduction,
                                        os.path.join(miniasm_dir, 'contained_reads.txt'))
    if keep >= 3:
        string_graph.save_to_gfa(os.path.join(miniasm_dir, '18_contigs_placed.gfa'))

    # TO DO: I can probably remove this line later, for efficiency. It's just a sanity check that
    # none of the graph manipulations screwed up the sequence ranges.
    string_graph.check_segment_names_and_ranges(read_dict, graph)

    # REMOVE NON-BRIDGING PATHS?

    # POLISH EACH BRIDGE SEQUENCE.
    # * For this we use the set of long reads which overlap the two single copy contigs on the
    #   correct side. It is not necessary for reads to overlap both contigs, as this will give us
    #   better coverage in the intervening repeat region.
    # * Use only the long read sequences, not the Illumina contigs. Since the Illumina contigs may
    #   not have been used all the way to their ends (slightly trimmed), this means a bit of contig
    #   sequence may be replaced by long read consensus.

    # TRY TO PLACE SMALLER SINGLE-COPY CONTIGS
    # * Use essentially the same process as place_isolated_contigs
    # *




    # LOOK FOR EACH BRIDGE SEQUENCE IN THE GRAPH.
    # * Goal 1: if we can find a short read version of the bridge, we should use that because it
    #   will probably be more accurate.
    # * Goal 2: using a graph path will let us 'use up' the segments, which helps with clean-up.
    # * In order to replace a miniasm assembly bridge sequence with a graph path sequence, the
    #   match has to be very strong! High identity over all sequence windows.
    # * Can use my existing path finding code, but tweak the settings to make them faster. This is
    #   because failing to find an existing path isn't too terrible, as we already have the miniasm
    #   sequence.

    # DO SOME BASIC GRAPH CLEAN-UP AND MERGE ALL POSSIBLE SEGMENTS.
    # * Clean up will be a bit tougher as we may have missed used sequence.

    # RE-RUN COPY NUMBER DETERMINATION.

    if keep < 3:
        shutil.rmtree(miniasm_dir)


def get_miniasm_assembly_reads(minimap_alignments, graph):
    """
    Returns a list of read names which overlap at least two different single copy graph segments.
    """
    miniasm_assembly_reads = []
    for read_name, alignments in minimap_alignments.items():
        overlap_count = 0
        for a in alignments:
            if a.overlaps_reference():
                seg = graph.segments[int(a.ref_name)]
                if segment_suitable_for_miniasm_assembly(graph, seg):
                    overlap_count += 1

        # TO DO: I'm not sure if this value should be 2 (only taking reads which span all the way
        # from one contig to the next) or 1 (also taking reads which overlap one contig but do not
        # reach the next). I should test each option and analyse.
        if overlap_count >= 1:
            miniasm_assembly_reads.append(read_name)
    return sorted(miniasm_assembly_reads)


def save_assembly_reads_to_file(read_filename, read_names, read_dict, graph, read_break_points):
    qual = chr(settings.CONTIG_READ_QSCORE + 33)
    log.log('Saving to ' + read_filename + ':')
    mean_read_quals = {}

    with open(read_filename, 'wt') as fastq:
        # First save the Illumina contigs as 'reads'. They are given a constant high qscore to
        # reflect our confidence in them.
        seg_count = 0
        for seg in sorted(graph.segments.values(), key=lambda x: x.number):
            if segment_suitable_for_miniasm_assembly(graph, seg):
                fastq.write('@CONTIG_')
                fastq.write(str(seg.number))
                fastq.write('\n')
                fastq.write(seg.forward_sequence)
                fastq.write('\n+\n')
                fastq.write(qual * seg.get_length())
                fastq.write('\n')
                seg_count += 1
        log.log('  ' + str(seg_count) + ' single copy contigs ' +
                str(settings.MIN_SEGMENT_LENGTH_FOR_MINIASM_BRIDGING) + ' bp or longer')

        # Now save the actual long reads.
        piece_count = 0
        for read_name in read_names:
            read = read_dict[read_name]

            if read_name not in read_break_points:
                breaks = [0, len(read.sequence)]
            else:
                breaks = [0] + sorted(list(read_break_points[read_name])) + [len(read.sequence)]
            read_ranges = [(breaks[i-1], breaks[i]) for i in range(1, len(breaks))]

            for i, read_range in enumerate(read_ranges):
                s, e = read_range
                if len(read_ranges) == 1:
                    read_range_name = read_name
                else:
                    read_range_name = read_name + '_range_' + str(s) + '-' + str(e)
                seq = read.sequence[s:e]
                quals = read.qualities[s:e]
                fastq.write('@')
                fastq.write(read_range_name)
                fastq.write('\n')
                fastq.write(seq)
                fastq.write('\n+\n')
                fastq.write(quals)
                fastq.write('\n')
                mean_read_quals[read_range_name] = statistics.mean(ord(x)-33 for x in quals)
                piece_count += 1

        break_string = ''
        if piece_count > len(read_names):
            break_string = ' broken into ' + str(piece_count) + ' pieces'

        log.log('  ' + str(len(read_names)) + ' overlapping long reads (out of ' +
                str(len(read_dict)) + ' total long reads)' + break_string)
    log.log('')
    return mean_read_quals


def segment_suitable_for_miniasm_assembly(graph, segment):
    """
    Returns True if the segment is:
      1) single copy
      2) long enough
      3) not already circular and complete
    """
    if graph.get_copy_number(segment) != 1:
        return False
    if segment.get_length() < settings.MIN_SEGMENT_LENGTH_FOR_MINIASM_BRIDGING:
        return False
    return not graph.is_component_complete([segment.number])

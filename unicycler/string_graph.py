"""
Copyright 2017 Ryan Wick (rrwick@gmail.com)
https://github.com/rrwick/Unicycler

This module describes a miniasm string graph and related functions. Unlike the AssemblyGraph class
(which hold a SPAdes-generated graph), a miniasm string graph does not have constant overlaps.

This file is part of Unicycler. Unicycler is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by the Free Software Foundation,
either version 3 of the License, or (at your option) any later version. Unicycler is distributed in
the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
details. You should have received a copy of the GNU General Public License along with Unicycler. If
not, see <http://www.gnu.org/licenses/>.
"""

import os
import statistics
from collections import defaultdict
from .misc import reverse_complement, add_line_breaks_to_sequence, range_overlap, green, red, \
    get_right_arrow
from .assembly_graph import build_reverse_links
from .cpp_wrappers import overlap_alignment, minimap_align_reads
from .minimap_alignment import load_minimap_alignments, combine_close_hits
from . import settings
from . import log


class StringGraph(object):

    def __init__(self, filename, mean_read_quals):
        self.segments = {}                      # unsigned seg name -> StringGraphSegment
        self.forward_links = defaultdict(list)  # signed seg name -> list of signed segment name
        self.reverse_links = defaultdict(list)  # signed seg name <- list of signed segment name
        self.links = {}                         # tuple (start, end) -> StringGraphLink

        # Load in the segments.
        with open(filename, 'rt') as gfa_file:
            for line in gfa_file:
                if line.startswith('S'):
                    line_parts = line.strip().split('\t')
                    name = line_parts[1]
                    sequence = line_parts[2]
                    self.segments[name] = StringGraphSegment(name, sequence, mean_read_quals)

        # Load in the links.
        with open(filename, 'rt') as gfa_file:
            for line in gfa_file:
                if line.startswith('L'):
                    line_parts = line.strip().split('\t')
                    signed_name_1 = line_parts[1] + line_parts[2]
                    signed_name_2 = line_parts[3] + line_parts[4]
                    self.forward_links[signed_name_1].append(signed_name_2)

                    link_tuple = (signed_name_1, signed_name_2)
                    if link_tuple not in self.links:
                        self.links[link_tuple] = StringGraphLink(signed_name_1, signed_name_2)
                    seg_1_to_seg_2_overlap = int(line_parts[5][:-1])
                    self.links[link_tuple].seg_1_overlap = seg_1_to_seg_2_overlap

                    rev_name_1 = flip_segment_name(signed_name_1)
                    rev_name_2 = flip_segment_name(signed_name_2)
                    rev_link_tuple = (rev_name_2, rev_name_1)
                    if rev_link_tuple not in self.links:
                        self.links[rev_link_tuple] = StringGraphLink(rev_name_2, rev_name_1)
                    self.links[rev_link_tuple].seg_2_overlap = seg_1_to_seg_2_overlap
            self.reverse_links = build_reverse_links(self.forward_links)

    def save_to_gfa(self, filename, verbosity=1, newline=False):
        """
        Saves whole graph to a GFA file.
        """
        log.log(('\n' if newline else '') + 'Saving ' + filename, verbosity)
        with open(filename, 'w') as gfa:
            for segment in sorted(self.segments.values(), key=lambda x: x.full_name):
                gfa.write(segment.gfa_segment_line())
            for link in sorted(self.links.keys()):
                gfa.write(self.links[link].gfa_link_line())

    def get_preceding_segments(self, seg_name):
        if seg_name not in self.reverse_links:
            return []
        return self.reverse_links[seg_name]

    def get_following_segments(self, seg_name):
        if seg_name not in self.forward_links:
            return []
        return self.forward_links[seg_name]

    def add_link(self, start, end, overlap_1, overlap_2):
        """
        Adds a link to the graph in all necessary ways: forward and reverse, and for reverse
        complements too.
        """
        rev_start = flip_segment_name(start)
        rev_end = flip_segment_name(end)

        if start not in self.forward_links:
            self.forward_links[start] = []
        if end not in self.forward_links[start]:
            self.forward_links[start].append(end)

        if end not in self.reverse_links:
            self.reverse_links[end] = []
        if start not in self.reverse_links[end]:
            self.reverse_links[end].append(start)

        if rev_start not in self.reverse_links:
            self.reverse_links[rev_start] = []
        if rev_end not in self.reverse_links[rev_start]:
            self.reverse_links[rev_start].append(rev_end)

        if rev_end not in self.forward_links:
            self.forward_links[rev_end] = []
        if rev_start not in self.forward_links[rev_end]:
            self.forward_links[rev_end].append(rev_start)

        link_tuple = (start, end)
        self.links[link_tuple] = StringGraphLink(start, end)
        self.links[link_tuple].seg_1_overlap = overlap_1
        self.links[link_tuple].seg_2_overlap = overlap_2

        rev_link_tuple = (rev_end, rev_start)
        self.links[rev_link_tuple] = StringGraphLink(rev_end, rev_start)
        self.links[rev_link_tuple].seg_1_overlap = overlap_2
        self.links[rev_link_tuple].seg_2_overlap = overlap_1

    def remove_segment(self, seg_name_to_remove):
        """
        Removes a segment from the graph and all of its related links.
        """
        def remove_signed_segment(graph, seg_name):
            for preceding_seg_name in graph.get_preceding_segments(seg_name):
                del graph.links[(preceding_seg_name, seg_name)]
                graph.forward_links[preceding_seg_name].remove(seg_name)
            for following_seg_name in graph.get_following_segments(seg_name):
                del graph.links[(seg_name, following_seg_name)]
                graph.reverse_links[following_seg_name].remove(seg_name)
            graph.forward_links.pop(seg_name, None)
            graph.reverse_links.pop(seg_name, None)

        remove_signed_segment(self, seg_name_to_remove + '+')
        remove_signed_segment(self, seg_name_to_remove + '-')
        self.segments.pop(seg_name_to_remove, None)

    def remove_branching_paths(self):
        log.log('', verbosity=2)
        log.log_explanation('Unicycler removes any links from the string graph which create '
                            'branches. I.e. if any segment has two or more links connected to one '
                            'end, those links are removed. This will result in a graph with only '
                            'simple linear paths that is suitable for creating unambiguous '
                            'bridges.', verbosity=2)
        # Put together a set of all links to be deleted.
        links_to_delete = set()
        for seg_name, segment in self.segments.items():
            pos_seg_name = seg_name + '+'
            neg_seg_name = seg_name + '-'
            following_segments = self.get_following_segments(pos_seg_name)
            preceding_segments = self.get_preceding_segments(pos_seg_name)
            if len(following_segments) > 1:
                for f in following_segments:
                    links_to_delete.add((pos_seg_name, f))
                    links_to_delete.add((flip_segment_name(f), neg_seg_name))
            if len(preceding_segments) > 1:
                for p in preceding_segments:
                    links_to_delete.add((p, pos_seg_name))
                    links_to_delete.add((neg_seg_name, flip_segment_name(p)))

        # Delete all links in the set in each possible way.
        deleted_links = []
        for link in sorted(list(links_to_delete)):
            if link in self.links:
                deleted_links.append(link)
                seg_1, seg_2 = link
                rev_seg_1 = flip_segment_name(seg_1)
                rev_seg_2 = flip_segment_name(seg_2)
                del self.links[(seg_1, seg_2)]
                self.forward_links[seg_1].remove(seg_2)
                self.reverse_links[seg_2].remove(seg_1)
                del self.links[(rev_seg_2, rev_seg_1)]
                self.forward_links[rev_seg_2].remove(rev_seg_1)
                self.reverse_links[rev_seg_1].remove(rev_seg_2)

        if deleted_links:
            log.log('Removed links:', verbosity=2)
            for seg_1, seg_2 in deleted_links:
                log.log('  ' + seg_1 + ' ' + get_right_arrow() + ' ' + seg_2, verbosity=2)
        else:
            log.log('No links needed removal', verbosity=2)


    def segment_leads_directly_to_contig_in_both_directions(self, seg_name):
        if self.segments[seg_name].contig:
            return True
        return (self.segment_leads_directly_to_contig(seg_name + '+') and
                self.segment_leads_directly_to_contig(seg_name + '-'))

    def segment_leads_directly_to_contig(self, signed_seg_name):
        """
        Tests whether a given segment leads to a contig via a simple unbranching path. Only tests
        in a single direction.
        """
        starting_seg_name = signed_seg_name
        current_seg_name = signed_seg_name
        while True:
            following_segments = self.get_following_segments(current_seg_name)
            preceding_segments = self.get_preceding_segments(current_seg_name)
            if len(following_segments) != 1 or len(preceding_segments) != 1:
                return False
            if self.segments[get_unsigned_seg_name(current_seg_name)].contig:
                return True
            current_seg_name = following_segments[0]
            if current_seg_name == starting_seg_name:  # Check if we've looped back to the start!
                return False

    def get_bridging_paths(self):
        """
        Returns a list of all bridging paths. The contigs being bridged are included at the start
        and end of each path.
        """
        paths = []
        used_segments = set()
        for seg_name, segment in self.segments.items():
            if not segment.contig and seg_name not in used_segments and \
                    self.segment_leads_directly_to_contig_in_both_directions(seg_name):
                starting_seg = seg_name + '+'
                current_seg = starting_seg
                path = [current_seg]
                while True:
                    current_seg = self.get_following_segments(current_seg)[0]
                    path.append(current_seg)
                    if self.segments[get_unsigned_seg_name(current_seg)].contig:
                        break
                current_seg = starting_seg
                while True:
                    current_seg = self.get_preceding_segments(current_seg)[0]
                    path.insert(0, current_seg)
                    if self.segments[get_unsigned_seg_name(current_seg)].contig:
                        break
                for seg in path:
                    used_segments.add(get_unsigned_seg_name(seg))
                paths.append(path)
        return paths

    def simplify_bridges(self, before_transitive_reduction):
        """
        This function takes cases where there is a simple path from one contig to the next, and it
        replaces it with a single spanning read.
        """
        log.log('', verbosity=2)
        log.log_explanation('The path between two contigs may contain many reads. If any one of '
                            'those reads spans the entire gap between the two contigs, then '
                            'Unicycler simplifies the graph by using only a single read to connect '
                            'the contigs. When multiple reads are available, it chooses the one '
                            'with the highest average qscore.',
                            verbosity=2)

        paths = self.get_bridging_paths()
        for path in paths:
            assert len(path) >= 3
            contig_1 = path[0]
            contig_2 = path[-1]
            middle = path[1:-1]

            # If there is only one read joining the contigs, then there's nothing to simplify.
            if len(middle) == 1:
                continue

            # If there are multiple reads, we hope to find one that connects the two contigs.
            # Search them in order of highest to lowest quality, seeing if the links we need are
            # in the pre-transitive reduction graph.
            single_bridge_read = None
            reads_by_qual = sorted([x for x in middle], reverse=True,
                               key=lambda x: self.segments[get_unsigned_seg_name(x)].qual)
            for read in reads_by_qual:
                if (contig_1, read) in before_transitive_reduction.links and \
                        (read, contig_2) in before_transitive_reduction.links:
                    single_bridge_read = read
                    break

            if single_bridge_read is not None:

                # Delete all of the other segments in the bridge.
                removed_segments = []
                for read in [x for x in middle if x != single_bridge_read]:
                    unsigned_read = get_unsigned_seg_name(read)
                    self.remove_segment(unsigned_read)
                    removed_segments.append(unsigned_read)
                log.log('Removed segments:  ' + ', '.join(removed_segments), verbosity=2)
                log.log('Surviving segment: ' + get_unsigned_seg_name(single_bridge_read),
                        verbosity=2)
                log.log('', verbosity=2)

                # Create links between the surviving segment and the contigs.
                link_1_tuple = (contig_1, single_bridge_read)
                link_2_tuple = (single_bridge_read, contig_2)
                if link_1_tuple not in self.links:
                    link_1 = before_transitive_reduction.links[link_1_tuple]
                    self.add_link(link_1_tuple[0], link_1_tuple[1],
                                  link_1.seg_1_overlap, link_1.seg_2_overlap)
                if link_2_tuple not in self.links:
                    link_2 = before_transitive_reduction.links[link_2_tuple]
                    self.add_link(link_2_tuple[0], link_2_tuple[1],
                                  link_2.seg_1_overlap, link_2.seg_2_overlap)


    def remove_overlaps(self, before_transitive_reduction, scoring_scheme):
        """
        Removes reads and trims reads to get rid of graph overlap:
          * Selectively removes overlaps from lower quality sequences first.
          * Keeps as much contig sequence as possible.

        before_transitive_reduction is the string graph at an earlier state where there are still
        transitive links. This helps us to find the correct overlaps when removing segments.
        This function assumes that remove_branching_paths has been run and we only have simple,
        unbranching paths to deal with.
        """
        log.log('', verbosity=2)
        log.log_explanation('At this point, the graph still contains overlaps - perhaps very long '
                            'overlaps. Unicycler now removes those from the graph by throwing out '
                            'redundant sequences and trimming sequence ends. As with the previous '
                            'step, sequences with higher average qscores are preferentially kept.',
                            verbosity=2)

        segments_by_quality = sorted([x for x in self.segments],
                                     key=lambda x: (self.segments[x].qual, self.segments[x].get_length()))
        for seg_name in segments_by_quality:
            seg = self.segments[seg_name]
            seg_len = seg.get_length()
            pos_seg_name = seg_name + '+'

            preceding_segments = self.get_preceding_segments(pos_seg_name)
            following_segments = self.get_following_segments(pos_seg_name)
            assert len(preceding_segments) <= 1
            assert len(following_segments) <= 1

            if len(preceding_segments) == 1:
                preceding_seg_name = preceding_segments[0]
                start_link = self.links[(preceding_seg_name, pos_seg_name)]
                start_overlap = start_link.seg_2_overlap
            else:
                preceding_seg_name = None
                start_link = None
                start_overlap = 0

            if len(following_segments) == 1:
                following_seg_name = following_segments[0]
                end_link = self.links[(pos_seg_name, following_seg_name)]
                end_overlap = end_link.seg_1_overlap
            else:
                following_seg_name = None
                end_link = None
                end_overlap = 0

            # If there aren't any overlaps, then we've nothing to do!
            if start_overlap == 0 and end_overlap == 0:
                continue

            # If the start and end overlap sum to less than the length of the segment, then we
            # trim off the overlaps and leave the segment in the middle. We do this by actually
            # removing the segment and creating a new one with a different start/end range.
            if start_overlap + end_overlap < seg_len:
                new_start_pos = seg.start_pos + start_overlap
                new_end_pos = seg.end_pos - end_overlap
                new_name = seg.short_name + ':' + str(new_start_pos) + '-' + str(new_end_pos)
                new_pos_name = new_name + '+'
                if end_overlap > 0:
                    new_seq = seg.forward_sequence[start_overlap:-end_overlap]
                else:
                    new_seq = seg.forward_sequence[start_overlap:]
                new_qual = seg.qual

                # Out with the old and in with the new!
                self.remove_segment(seg_name)
                self.segments[new_name] = StringGraphSegment(new_name, new_seq, qual=new_qual)
                if preceding_seg_name is not None:
                    self.add_link(preceding_seg_name, new_pos_name, 0, 0)
                if following_seg_name is not None:
                    self.add_link(new_pos_name, following_seg_name, 0, 0)

                log.log('trimmed ' + str(start_overlap) + ' bp from start and ' +
                        str(end_overlap) + ' bp from end of ' + seg_name, verbosity=2)

            # If the start and end overlap are more than the length of the segment, then we can
            # remove the segment entirely (because the preceding and following segments will
            # still overlap).
            elif start_link is not None and end_link is not None:
                start_overlap_ratio = start_link.seg_1_overlap / start_link.seg_2_overlap
                end_overlap_ratio = end_link.seg_2_overlap / end_link.seg_1_overlap

                # Make a guess for the new link overlaps.
                new_link_overlap = -(seg_len - start_overlap - end_overlap)
                overlap_1 = int(round(new_link_overlap * start_overlap_ratio))
                overlap_2 = int(round(new_link_overlap * end_overlap_ratio))

                # Look for the link in the before transitive reduction graph to find the
                # exact overlaps, they are available.
                link_tuple = (preceding_seg_name, following_seg_name)
                if link_tuple in before_transitive_reduction.links:
                    exact_link = before_transitive_reduction.links[link_tuple]
                    overlap_1 = exact_link.seg_1_overlap
                    overlap_2 = exact_link.seg_2_overlap

                # If we failed to find the link in the graph, we will align the reads
                # using to get an exact overlap.
                else:
                    preceding_segment_seq = self.seq_from_signed_seg_name(preceding_seg_name)
                    following_segment_seq = self.seq_from_signed_seg_name(following_seg_name)
                    guess_overlap = max(overlap_1, overlap_2)
                    new_overlap_1, new_overlap_2 = overlap_alignment(preceding_segment_seq,
                                                                     following_segment_seq,
                                                                     scoring_scheme,
                                                                     guess_overlap)
                    if new_overlap_1 != -1 and new_overlap_2 != 1:
                        overlap_1, overlap_2 = new_overlap_1, new_overlap_2

                self.add_link(preceding_seg_name, following_seg_name, overlap_1, overlap_2)
                self.remove_segment(seg_name)
                log.log('removed ' + seg_name, verbosity=2)

            # We should never fail both of the above conditions. I.e. either we can trim the ends
            # or there is a link on both sides. Neither shouldn't be true (a link on one side
            # which covers the whole read) because that would imply that the read is contained,
            # something that miniasm filters out.
            else:
                assert False

        # We should now have an overlap-free graph!
        self.check_graph_has_no_overlaps()
        log.log('', verbosity=2)

    def merge_reads(self):
        """
        This function takes any reads in a simple path and merges them together. It assumes that
        the graph is now overlap-free.
        """
        log.log('', verbosity=2)
        log.log_explanation('Simple paths in the graph which contain only long read sequences can '
                            'now be merged together. This results in a graph with two distinct '
                            'types of segments: contigs (from the short read assembly) and long '
                            'read sequences.',
                            verbosity=2)

        segments_in_paths = set()
        paths_to_merge = []
        for seg_name in sorted(self.segments.keys(), reverse=True,
                               key=lambda x: self.segments[x].get_length()):
            if seg_name in segments_in_paths:
                continue
            path = self.get_simple_read_path(seg_name + '+')
            if path:
                segments_in_paths.update(get_unsigned_seg_name(x) for x in path)
            if len(path) > 1:
                paths_to_merge.append(path)

        for path in paths_to_merge:
            merged_seg_name = self.get_next_available_merged_segment_name()
            merged_seg_seq = ''
            merged_seq_quals = []
            for path_seg in path:
                seq = self.seq_from_signed_seg_name(path_seg)
                merged_seq_quals.append(self.segments[get_unsigned_seg_name(path_seg)].qual)
                merged_seg_seq += seq
            merged_seq_qual = statistics.mean(merged_seq_quals)
            self.segments[merged_seg_name] = StringGraphSegment(merged_seg_name, merged_seg_seq,
                                                                qual=merged_seq_qual)
            pos_merged_seg_name = merged_seg_name + '+'

            preceding_segments = self.get_preceding_segments(path[0])
            following_segments = self.get_following_segments(path[-1])
            assert len(preceding_segments) <= 1
            assert len(following_segments) <= 1

            # If the path is circular, then we just need to make one new link: start to end.
            if (len(preceding_segments) == 1 and len(following_segments) == 1 and
                    preceding_segments[0] == path[-1] and following_segments[0] == path[0]):
                self.add_link(pos_merged_seg_name, pos_merged_seg_name, 0, 0)

            # If the path isn't circular, then we need to link it up to any neighbours it might
            # have (which must be contigs).
            else:
                if len(preceding_segments) == 1:
                    preceding_segment = preceding_segments[0]
                    assert preceding_segment.startswith('CONTIG_')
                    self.add_link(preceding_segment, pos_merged_seg_name, 0, 0)
                if len(following_segments) == 1:
                    following_segment = following_segments[0]
                    assert following_segment.startswith('CONTIG_')
                    self.add_link(pos_merged_seg_name, following_segment, 0, 0)

            log.log((' ' + get_right_arrow() + ' ').join(path), verbosity=2)
            log.log('merged into ' + merged_seg_name, verbosity=2)
            log.log('', verbosity=2)

            for path_seg in path:
                self.remove_segment(get_unsigned_seg_name(path_seg))
        log.log('', verbosity=2)

    def get_next_available_merged_segment_name(self):
        n = 1
        while True:
            name = 'merged_reads_' + str(n)
            if name not in self.segments:
                return name
            n += 1

    def get_simple_read_path(self, starting_seg):
        """
        Starting from the given segment, this simple paths consisting only of reads (not contigs).
        """
        if self.segments[get_unsigned_seg_name(starting_seg)].contig:
            return []
        simple_path = [starting_seg]

        # Extend path forward as much as possible.
        current_seg_name = starting_seg
        while True:
            following_segments = self.get_following_segments(current_seg_name)
            if len(following_segments) != 1:
                break
            current_seg_name = following_segments[0]
            if self.segments[get_unsigned_seg_name(current_seg_name)].contig:
                break
            if current_seg_name == starting_seg:  # Check for looping in a circle
                return simple_path
            simple_path.append(current_seg_name)

        # Extend path backward as much as possible.
        current_seg_name = starting_seg
        while True:
            preceding_segments = self.get_preceding_segments(current_seg_name)
            if len(preceding_segments) != 1:
                break
            current_seg_name = preceding_segments[0]
            if self.segments[get_unsigned_seg_name(current_seg_name)].contig:
                break
            simple_path = [current_seg_name] + simple_path

        return simple_path

    def seq_from_signed_seg_name(self, signed_name):
        assert(signed_name.endswith('+') or signed_name.endswith('-'))
        unsigned_seg_name = get_unsigned_seg_name(signed_name)
        if signed_name.endswith('+'):
            return self.segments[unsigned_seg_name].forward_sequence
        else:
            return self.segments[unsigned_seg_name].reverse_sequence

    def save_non_contigs_to_file(self, filename, min_length):
        """
        Saves all graph segments which are not short read contigs to a FASTA file.
        """
        log.log('Saving ' + filename, 1)
        with open(filename, 'w') as fasta:
            for segment in sorted(self.segments.values(), reverse=True,
                                  key=lambda x: x.get_length()):
                if segment.contig or segment.get_length() < min_length:
                    continue
                fasta.write(segment.fasta_record())

    def save_isolated_contigs_to_file(self, filename, contained_reads_filename,
                                      before_transitive_reduction):
        """
        Saves all graph segments which are contigs but with no connections to a FASTA file.
        Specifically, it only saves contigs which:
          1) have been excluded by miniasm because they were contained (a list of such reads is in
             contained_reads.txt)
          2) were connected in the graph before transitive reduction but are no longer (implying
             that they fell out in some part of the graph simplification process).
        """
        contained_contig_names = set()
        with open(contained_reads_filename, 'rt') as contained_reads_file:
            for line in contained_reads_file:
                if line.startswith('CONTIG_'):
                    contained_contig_names.add(line.strip())

        has_connections_before_transitive_reduction = set()
        for seg_name, segment in before_transitive_reduction.segments.items():
            pos_seg_name = seg_name + '+'
            if (len(before_transitive_reduction.get_preceding_segments(pos_seg_name)) > 0 or
                    len(before_transitive_reduction.get_following_segments(pos_seg_name)) > 0):
                has_connections_before_transitive_reduction.add(segment.short_name)

        log.log('Saving ' + filename, 1)
        with open(filename, 'w') as fasta:
            for segment in sorted(self.segments.values(), reverse=True,
                                  key=lambda x: x.get_length()):
                if not segment.contig:
                    continue
                pos_seg_name = segment.full_name + '+'
                if len(self.get_preceding_segments(pos_seg_name)) > 0 or \
                        len(self.get_following_segments(pos_seg_name)) > 0:
                    continue
                if (segment.short_name in contained_contig_names or
                        segment.short_name in has_connections_before_transitive_reduction):
                    fasta.write(segment.fasta_record())

    def place_isolated_contigs(self, working_dir, threads, before_transitive_reduction,
                               contained_reads_filename):
        log.log('', verbosity=2)
        log.log_explanation('Some single copy contigs may be isolated in the graph because they '
                            'were contained in long reads (because miniasm filters out contained '
                            'sequences). Others may be isolated due to some part of the graph '
                            'simplification (e.g. lost in bubble popping). Since single copy '
                            'contigs definitely belong in the assembly, Unicycler now tries to '
                            'find their corresponding sequence in a long read graph segment to '
                            'place these contigs back into the main graph.',
                            verbosity=2)

        isolated_contigs_file = os.path.join(working_dir, '16_isolated_contigs.fasta')
        self.save_isolated_contigs_to_file(isolated_contigs_file, contained_reads_filename,
                                           before_transitive_reduction)
        non_contigs_file = os.path.join(working_dir, '17_non-contigs.fasta')
        self.save_non_contigs_to_file(non_contigs_file,
                                      settings.MIN_SEGMENT_LENGTH_FOR_MINIASM_BRIDGING / 2)

        log.log('Searching for isolated contigs with minimap')
        contig_to_read_alignments = \
            load_minimap_alignments(minimap_align_reads(non_contigs_file, isolated_contigs_file,
                                                        threads, 3, 'find contigs'))
        contig_alignments_by_segment = defaultdict(list)
        isolated_contig_names = sorted(contig_to_read_alignments.keys(), reverse=True,
                                       key=lambda x: self.segments[x].get_length())
        log.log('\n' + 'Isolated contigs: ', verbosity=2)
        for contig_name in isolated_contig_names:
            alignments = contig_to_read_alignments[contig_name]
            alignments = combine_close_hits(alignments, settings.FOUND_CONTIG_MIN_RATIO,
                                            settings.FOUND_CONTIG_MAX_RATIO, self)
            if alignments:
                alignments = sorted(alignments, key=lambda x: x.matching_bases)
                best = alignments[-1]

                # If there are multiple possible places to put the contig, we check to see if the
                # second-best option is too close to the best. If so, we don't place it.
                second_best_bases = alignments[-2].matching_bases if len(alignments) > 1 else 0
                second_to_first = second_best_bases / best.matching_bases

                if best.fraction_read_aligned() < settings.MIN_FOUND_CONTIG_FRACTION:
                    log.log('  ' + contig_name + ': ' + red('not found'))
                elif second_to_first >= settings.FOUND_CONTIG_SECOND_BEST_THRESHOLD:
                    log.log('  ' + contig_name + ': ' + red('found in multiple places'))
                else:
                    contig_alignments_by_segment[best.ref_name].append(best)
                    log.log('  ' + contig_name + ': ' + green('found in ' + best.ref_name))
            else:
                log.log('  ' + contig_name + ': ' + red('not found'))

        log.log('', verbosity=2)
        if len(contig_alignments_by_segment) == 0:
            log.log(red('No isolated contigs could be placed in the graph.'), verbosity=2)
            return

        log.log_explanation('Now graph segments which contain single copy contigs are chopped into '
                            'pieces with the contigs replacing their corresponding sequence. This '
                            'makes a string graph which contains as many of the single copy '
                            'contigs as possible (ideally all of them).',
                            verbosity=2)
        for seg_name in sorted(contig_alignments_by_segment.keys(), reverse=True,
                               key=lambda x: self.segments[x].get_length()):
            seg = self.segments[seg_name]

            # Check to see if this segment is circular, because if so we'll need to create a
            # circularising link later.
            circular = self.segment_is_circular(seg_name)

            # Make sure none of the alignments overlap on the segment.
            alignments = []
            for a in sorted(contig_alignments_by_segment[seg_name], key=lambda x: x.matching_bases):
                if not any(range_overlap(a.ref_start, a.ref_end, b.ref_start, b.ref_end)
                           for b in alignments):
                    alignments.append(a)
            alignments = sorted(contig_alignments_by_segment[seg_name], key=lambda x: x.ref_start)

            log.log('Old segment:  ' + seg_name, verbosity=2)

            # Get the neighbouring segments for later when we make the links.
            preceding_segments = self.get_preceding_segments(seg_name + '+')
            following_segments = self.get_following_segments(seg_name + '+')
            assert len(preceding_segments) <= 1
            assert len(following_segments) <= 1
            if len(preceding_segments) == 1:
                preceding_segment = preceding_segments[0]
            else:
                preceding_segment = None
            if len(following_segments) == 1:
                following_segment = following_segments[0]
            else:
                following_segment = None

            piece_names, signed_piece_names, piece_seqs, piece_reverse = [], [], [], []
            full_seg_seq = seg.forward_sequence
            current_pos = 0
            for i, a in enumerate(alignments):

                # TO DO: if this segment is circular, then check to see if any alignments loop
                #        around the link (as indicated by a negative ref_start or a negative
                #        ref_end_gap). If so, make sure there is only one (i.e. we don't have two
                #        overlapping alignments that loop, one start and one end) and deal with it
                #        accordingly.

                # First get the piece of the read segment.
                piece_start_pos = seg.start_pos + current_pos
                piece_end_pos = seg.start_pos + a.ref_start - 1
                piece_name = seg.short_name + ':' + str(piece_start_pos) + '-' + str(piece_end_pos)
                piece_seq = full_seg_seq[current_pos:a.ref_start]
                if len(piece_seq) > 0:
                    piece_names.append(piece_name)
                    signed_piece_names.append(piece_name + '+')
                    piece_seqs.append(piece_seq)
                    piece_reverse.append(False)

                # Now put in the contig segment.
                signed_full_seq_name = a.read_name + a.read_strand
                full_contig_seq = self.seq_from_signed_seg_name(signed_full_seq_name)
                contig_name, contig_seq = \
                    get_adjusted_contig_name_and_seq(signed_full_seq_name, full_contig_seq,
                                                     a.read_start, a.read_end)
                reverse = contig_name.endswith('-')
                if reverse:
                    contig_seq = reverse_complement(contig_seq)
                contig_name = contig_name[:-1]  # Remove sign from name

                # Delete the isolated contig from the graph.
                self.remove_segment(a.read_name)

                piece_names.append(contig_name)
                signed_piece_names.append(contig_name + ('-' if reverse else '+'))
                piece_seqs.append(contig_seq)
                piece_reverse.append(reverse)

                current_pos = a.ref_end

            # Make a segment for the read segment piece after the last contig.
            piece_start_pos = seg.start_pos + current_pos
            piece_name = seg.short_name + ':' + str(piece_start_pos) + '-' + str(seg.end_pos)
            piece_seq = full_seg_seq[current_pos:]
            if len(piece_seq) > 0:
                piece_names.append(piece_name)
                signed_piece_names.append(piece_name + '+')
                piece_seqs.append(piece_seq)
                piece_reverse.append(False)

            log.log('New segments: ' + ', '.join(signed_piece_names), verbosity=2)

            # Create the segments and link them together.
            first_piece, last_piece = None, None
            for i, name in enumerate(piece_names):
                seq = piece_seqs[i]
                sign = '-' if piece_reverse[i] else '+'
                self.segments[name] = StringGraphSegment(name, seq, qual=seg.qual)

                if i == 0:
                    first_piece = name + sign
                    if preceding_segment is not None:
                        self.add_link(preceding_segment, name + sign, 0, 0)
                else:
                    prev_name = piece_names[i-1]
                    prev_sign = '-' if piece_reverse[i-1] else '+'
                    self.add_link(prev_name + prev_sign, name + sign, 0, 0)

                if i == len(piece_names) - 1 and following_segment is not None:  # last piece
                    last_piece = name + sign
                    self.add_link(name + sign, following_segment, 0, 0)

            if circular:
                self.add_link(last_piece, first_piece, 0, 0)

            # Delete the old read segment which we've replaced.
            self.remove_segment(seg_name)

            log.log('', verbosity=2)

    def check_graph_has_no_overlaps(self):
        """
        Asserts that the graph has no branching structures and no overlaps.
        """
        for seg_name in self.segments.keys():
            pos_seg_name = seg_name + '+'
            neg_seg_name = seg_name + '-'
            preceding_segments = self.get_preceding_segments(pos_seg_name)
            following_segments = self.get_following_segments(pos_seg_name)
            assert len(preceding_segments) < 2
            assert len(following_segments) < 2
            if len(preceding_segments) == 1:
                preceding_seg_name = preceding_segments[0]
                start_link = self.links[(preceding_seg_name, pos_seg_name)]
                rev_start_link = self.links[(neg_seg_name, flip_segment_name(preceding_seg_name))]
                assert start_link.seg_1_overlap == 0
                assert start_link.seg_2_overlap == 0
                assert rev_start_link.seg_1_overlap == 0
                assert rev_start_link.seg_2_overlap == 0
            if len(following_segments) == 1:
                following_seg_name = following_segments[0]
                end_link = self.links[(pos_seg_name, following_seg_name)]
                rev_end_link = self.links[(flip_segment_name(following_seg_name), neg_seg_name)]
                assert end_link.seg_1_overlap == 0
                assert end_link.seg_2_overlap == 0
                assert rev_end_link.seg_1_overlap == 0
                assert rev_end_link.seg_2_overlap == 0

    def check_segment_names_and_ranges(self, read_dict, assembly_graph):
        """
        This function looks at the string graph segment names and makes sure that their ranges
        match up with the original sequences. It is to ensure that we haven't screwed anything up
        in the various string graph manipulations we've done.
        """
        for seg_name, seg in self.segments.items():
            assert seg_name == seg.full_name

            # If the string graph segment name contains a range, make sure it matches up with the
            # range in the object.
            try:
                range_in_name = [int(x) for x in seg_name.rsplit(':', 1)[1].split('-')]
            except (IndexError, ValueError):
                range_in_name = [None, None]
            if range_in_name[0] is None:
                assert seg.short_name == seg.full_name
            else:
                assert range_in_name[0] == seg.start_pos
                assert range_in_name[1] == seg.end_pos
                assert seg.short_name == seg_name.rsplit(':', 1)[0]

            if seg_name.startswith('CONTIG_'):
                seg_num = int(seg_name[7:].split(':')[0])
                full_seq = assembly_graph.seq_from_signed_seg_num(seg_num)
            elif seg.short_name in read_dict:  # the segment is a long read
                full_seq = read_dict[seg.short_name].sequence
            else:  # We can't check split or merged reads, so they are skipped.
                full_seq = None

            if full_seq is not None:
                # Miniasm uses 1-based inclusive ranges
                assert seg.forward_sequence == full_seq[seg.start_pos-1:seg.end_pos]

    def segment_is_circular(self, seg_name):
        """
        Returns whether or not the segment has a circularising link.
        """
        pos_seg_name = seg_name + '+'
        preceding_segments = self.get_preceding_segments(pos_seg_name)
        following_segments = self.get_following_segments(pos_seg_name)
        if len(preceding_segments) != 1 or len(following_segments) != 1:
            return False
        preceding_seg_name = preceding_segments[0]
        following_seg_name = following_segments[0]
        return preceding_seg_name == pos_seg_name and following_seg_name == pos_seg_name


class StringGraphSegment(object):

    def __init__(self, full_name, sequence, mean_read_quals=None, qual=None):
        self.full_name = full_name
        self.forward_sequence = sequence
        self.reverse_sequence = reverse_complement(sequence)

        # Miniasm trims reads and puts the start/end positions in the name...
        try:
            name_parts = full_name.rsplit(':', 1)
            self.short_name = name_parts[0]
            self.start_pos, self.end_pos = (int(x) for x in name_parts[1].split('-'))

        # ..but ranges don't apply to some graph segments, like 'merged_reads' segments.
        except (IndexError, ValueError):
            self.short_name = self.full_name
            self.start_pos, self.end_pos = 1, len(self.forward_sequence)

        if self.short_name.startswith('CONTIG_'):
            self.contig = True
            self.qual = settings.CONTIG_READ_QSCORE
        elif mean_read_quals is not None:
            assert(self.short_name in mean_read_quals)  # if not a contig, it should be a real long read
            self.contig = False
            self.qual = mean_read_quals[self.short_name]
        else:
            self.contig = False
            self.qual = None

        # If the construct gave an explicit quality, we'll use that.
        if qual is not None:
            self.qual = qual

    def __repr__(self):
        if len(self.forward_sequence) > 6:
            seq_string = (self.forward_sequence[:3] + '...' + self.forward_sequence[-3:] + ', ' +
                          str(len(self.forward_sequence)) + ' bp')
        else:
            seq_string = self.forward_sequence
        return self.full_name + ' (' + seq_string + '), mean score = ' + str(self.qual)

    def get_length(self):
        return len(self.forward_sequence)

    def gfa_segment_line(self):
        return '\t'.join(['S', self.full_name, self.forward_sequence]) + '\n'

    def fasta_record(self):
        return ''.join(['>', self.full_name, '\n',
                        add_line_breaks_to_sequence(self.forward_sequence, 70)])


class StringGraphLink(object):

    def __init__(self, seg_1_signed_name, seg_2_signed_name):
        self.seg_1_signed_name = seg_1_signed_name
        self.seg_2_signed_name = seg_2_signed_name
        self.seg_1_overlap = None
        self.seg_2_overlap = None

    def __repr__(self):
        return (self.seg_1_signed_name + ' -> ' + self.seg_2_signed_name + ' (' +
                str(self.seg_1_overlap) + ', ' + str(self.seg_2_overlap) + ')')

    def gfa_link_line(self):
        seg_1_name = get_unsigned_seg_name(self.seg_1_signed_name)
        seg_1_sign = self.seg_1_signed_name[-1]
        seg_2_name = get_unsigned_seg_name(self.seg_2_signed_name)
        seg_2_sign = self.seg_2_signed_name[-1]
        overlap = str(self.seg_1_overlap) + 'M'
        return '\t'.join(['L', seg_1_name, seg_1_sign, seg_2_name, seg_2_sign, overlap]) + '\n'


def flip_segment_name(seg_name):
    assert(seg_name.endswith('+') or seg_name.endswith('-'))
    if seg_name.endswith('+'):
        return get_unsigned_seg_name(seg_name) + '-'
    else:
        return get_unsigned_seg_name(seg_name) + '+'


def get_unsigned_seg_name(seg_name):
    assert(seg_name.endswith('+') or seg_name.endswith('-'))
    return seg_name[:-1]


def get_adjusted_contig_name_and_seq(contig_name, full_seq, start_pos, end_pos):
    """
    This function adjusts the start/end positions in the contig name. It's used when a partial
    contig (from miniasm) has a partial alignment (from minimap).
    """
    sign = contig_name[-1]
    name_parts = contig_name[:-1].rsplit(':', 1)
    base_name = name_parts[0]
    old_start, _ = (int(x) for x in name_parts[1].split('-'))
    new_start = old_start + start_pos
    new_end = (end_pos - start_pos) + new_start - 1

    if sign == '-':
        new_start_pos = len(full_seq) - end_pos
        new_end_pos = len(full_seq) - start_pos
        start_pos, end_pos = new_start_pos, new_end_pos
    adjusted_name = base_name + ':' + str(new_start) + '-' + str(new_end) + sign
    adjusted_seq = full_seq[start_pos:end_pos]

    return adjusted_name, adjusted_seq

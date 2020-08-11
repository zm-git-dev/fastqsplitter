#!/usr/bin/env python3

# Copyright (c) 2019 Leiden University Medical Center
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import argparse
import contextlib
import io
import os
from typing import List

# xopen opens files as normal files, gzip files, bzip2 files or xz files
# depending on extension.
import xopen

# Choose 1 as default compression level. Speed is more important than filesize
# in this application.
DEFAULT_COMPRESSION_LEVEL = 1
DEFAULT_BUFFER_SIZE = 64 * 1024
# With one thread per file (pigz -p 1) pigz uses way less virtual memory
# (10 vs 300 MB for 4 threads) and total CPU time is also decreased.
# Example: 2.3 gb input file file. Five output files.
# TT=Total cpu time. RT=realtime
# Compression level 1: threads=1 RT=58s TT=3m33, threads=4 RT=57s TT=3m45
# Compression level 5: threads=1 RT=1m48 TT=7m53, threads=4 RT=1m23, TT=8m39
# But this is on a 8 thread machine. When using less threads RT will go towards
# TT. Default compression is 1. So default threads 1 makes the most sense.
DEFAULT_THREADS_PER_FILE = 1
DEFAULT_SUFFIX = ".fastq.gz"
DEFAULT_INPUT = "/dev/stdin" if os.name == "posix" else None


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=str, default=DEFAULT_INPUT,
                        help="The fastq file to be scattered.")
    parser.add_argument("-p", "--prefix", type=str,
                        help="The prefix for the output files.")
    parser.add_argument("-s", "--suffix", type=str, default=DEFAULT_SUFFIX,
                        help="The default suffix for the output files. The "
                             "extension determines which compression is used. "
                             "'.gz' for gzip, '.bz2' for bzip2, '.xz' for xz. "
                             "Other extensions will use no compression.")
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument("-n", "--number", type=int,
                              help="Specify the number of output files which "
                                   "to split over. Fastq records will be "
                                   "distributed using a round-robin method.")
    output_group.add_argument(
        "-m", "--max-size", type=str,
        help="Split the fastq files in chunks of this max size. Accepts "
             "suffixes 'K', 'M' and 'G'. NOTE: This is the size *before* "
             "compression (if applied). As a rule of thumb multiply by 0.38 "
             "to get the actual filesize when using gzip compression.")
    output_group.add_argument(
        "-o", "--output", action="append", type=str,
        help="Scatter over these output files. Multiple -o flags can be used. "
             "The extensions determine which compression algorithm will be "
             "used. '.gz' for gzip, '.bz2' for bzip2, '.xz' for xz. Other "
             "extensions will use no compression. Fastq records will be "
             "distributed using a round-robin method.")

    parser.add_argument("-c", "--compression-level", type=int,
                        default=DEFAULT_COMPRESSION_LEVEL,
                        help="Only applicable when output files have a '.gz' "
                             "extension. Default={0}"
                        .format(DEFAULT_COMPRESSION_LEVEL))
    parser.add_argument("-t", "--threads-per-file", type=int,
                        default=DEFAULT_THREADS_PER_FILE,
                        help="Set the number of compression threads per output"
                             " file. NOTE: more threads are only useful when "
                             "using a compression level > 1. Default={0}"
                             "".format(DEFAULT_THREADS_PER_FILE))

    # BELOW ARGUMENTS ARE FOR BENCHMARKING AND TESTING PURPOSES
    parser.add_argument("-b", "--buffer-size", type=int,
                        default=DEFAULT_BUFFER_SIZE,
                        help=argparse.SUPPRESS)
    return parser


def filesplitter(input_handle: io.BufferedReader,
                 output_handles: List[io.BufferedWriter],
                 lines_per_record: int = 4,
                 buffer_size: int = 64 * 1024):

    # Make sure inputs are sensible.
    if len(output_handles) < 1:
        raise ValueError("The number of output files should be at least 1.")
    if lines_per_record < 1:
        raise ValueError("The number of lines per record should be at least 1."
                         )
    if buffer_size < 1024:
        # This value is arbitrary, but really low values such as 5 or 30 don't
        # make sense.
        raise ValueError("The buffer size should be at least 1024.")

    group_number = 0
    number_of_output_files = len(output_handles)

    while True:
        # this can also be achieved by a for loop and an iter function. Which
        # is more concise, but less readable. It does not matter for speed.
        read_buffer = input_handle.read(buffer_size)
        if read_buffer == b"":
            return

        newline_count = read_buffer.count(b'\n')

        # The chances are paramount that our buffer does not end with \n.
        # The buffer almost always ends  with an incomplete record. Therefore
        # we read all the missing lines. Please note that if
        # newline_count % lines_per_record == 0. That probably means we still
        # have a start of a record after the last \n.
        missing_newlines = lines_per_record - newline_count % lines_per_record
        completed_record = b"".join(input_handle.readline()
                                    for _ in range(missing_newlines))
        output_handles[group_number].write(read_buffer + completed_record)
        # Set the group number for the next group to be written.
        group_number += 1
        if group_number == number_of_output_files:
            group_number = 0


def split_fastqs(input_file: str, output_files: List[str],
                 compression_level: int = DEFAULT_COMPRESSION_LEVEL,
                 buffer_size: int = DEFAULT_BUFFER_SIZE,
                 threads_per_file: int = DEFAULT_THREADS_PER_FILE):
    # contextlib.Exitstack allows us to open multiple files at once which
    # are automatically closed on error.
    # https://stackoverflow.com/questions/19412376/open-a-list-of-files-using-with-as-context-manager
    with contextlib.ExitStack() as stack:
        input_fastq = stack.enter_context(
            xopen.xopen(input_file, mode='rb'))
        output_handles = [
            stack.enter_context(xopen.xopen(
                filename=output_file,
                mode='wb',
                compresslevel=compression_level,
                threads=threads_per_file
            )) for output_file in output_files
        ]  # type: List[io.BufferedWriter]
        filesplitter(input_handle=input_fastq,
                     output_handles=output_handles,
                     buffer_size=buffer_size,
                     lines_per_record=4)


def read_chunk_to_file(input_handle: io.BufferedReader,
                       output_handle: io.BufferedReader,
                       max_size: int,
                       lines_per_record: int = 1,
                       buffer_size: int = DEFAULT_BUFFER_SIZE) -> int:
    target_size = max_size - buffer_size
    total_size = 0
    total_newlines = 0
    while True:
        read_buffer = input_handle.read(buffer_size)
        if read_buffer == b"":
            return total_size
        output_handle.write(read_buffer)
        total_newlines += read_buffer.count(b'\n')
        total_size += buffer_size
        if total_size >= target_size:
            # Complete the record
            missing_newlines = (lines_per_record -
                                total_newlines % lines_per_record)
            completed_record = b"".join(input_handle.readline()
                                        for _ in range(missing_newlines))
            output_handle.write(completed_record)
            return total_size + len(completed_record)


def chunk_fastqs(input_file: str,
                 max_size: int,
                 prefix: str = "split.",
                 suffix: str = DEFAULT_SUFFIX,
                 buffer_size: int = DEFAULT_BUFFER_SIZE,
                 compression_level: int = DEFAULT_COMPRESSION_LEVEL,
                 threads_per_file: int = DEFAULT_THREADS_PER_FILE):
    if max_size < buffer_size:
        raise ValueError("Maximum size {0} should be larger than buffer size "
                         "{1}.".format(max_size, buffer_size))

    with xopen.xopen(input_file, mode="rb") as input_fastq:
        group_number = 0
        written_files: List[str] = []
        while True:
            if input_fastq.peek(0) == b"":  # Quit if there are no bytes left
                return written_files
            filename = prefix + str(group_number) + suffix
            group_number += 1  # Increase group_number for the next file
            with xopen.xopen(filename, mode="wb",
                             compresslevel=compression_level,
                             threads=threads_per_file) as output_fastq:
                read_chunk_to_file(input_fastq, output_fastq,
                                   max_size,
                                   lines_per_record=4,
                                   buffer_size=buffer_size)
                written_files.append(filename)


def main():
    parser = argument_parser()
    args = parser.parse_args()
    default_prefix = os.path.basename(args.input)
    default_prefix.rstrip(".gz")
    default_prefix.rstrip(".fastq")
    default_prefix.rstrip(".fq")
    prefix = args.prefix if args.prefix else default_prefix

    if args.output or args.number:
        if args.output:
            output = args.output
        else:
            output = [prefix + str(i) + args.suffix for i in range(args.number)
                      ]
        split_fastqs(args.input,
                     output,
                     compression_level=args.compression_level,
                     threads_per_file=args.threads_per_file,
                     buffer_size=args.buffer_size)
    else:
        chunk_fastqs(input_file=args.input,
                     max_size=args.max_size,
                     prefix=prefix,
                     suffix=args.suffix,
                     buffer_size=args.buffer_size,
                     compression_level=args.compression_level,
                     threads_per_file=args.threads_per_file)


if __name__ == "__main__":  # pragma: no cover
    main()

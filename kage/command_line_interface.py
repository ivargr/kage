import logging
#logging.basicConfig(level=logging.INFO, format='%(module)s %(asctime)s %(levelname)s: %(message)s')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
import itertools
from itertools import repeat
import sys, argparse, time
from graph_kmer_index.shared_mem import from_shared_memory, to_shared_memory, remove_shared_memory, SingleSharedArray, remove_all_shared_memory, remove_shared_memory_in_session
from obgraph import Graph as ObGraph
from graph_kmer_index import KmerIndex, ReverseKmerIndex
from graph_kmer_index import ReferenceKmerIndex
from .analysis import GenotypeDebugger
from obgraph.variants import VcfVariants, TruthRegions
from obgraph.haplotype_nodes import HaplotypeToNodes
from .reads import read_chunks_from_fasta
import platform
from pathos.multiprocessing import Pool
import numpy as np
from .node_counts import NodeCounts
from .node_count_model import NodeCountModel, GenotypeNodeCountModel, NodeCountModelAlleleFrequencies, NodeCountModelAdvanced, NodeCountModelCreatorAdvanced
from obgraph.genotype_matrix import MostSimilarVariantLookup, GenotypeFrequencies, GenotypeTransitionProbabilities
from obgraph.variant_to_nodes import VariantToNodes
from .genotyper import Genotyper
from .numpy_genotyper import NumpyGenotyper
from .combination_model_genotyper import CombinationModelGenotyper
from .bayes_genotyper import NewBayesGenotyper
import SharedArray as sa
from obgraph.haplotype_matrix import HaplotypeMatrix
from obgraph.variant_to_nodes import NodeToVariants
import random
from obgraph.genotype_matrix import GenotypeMatrix
from .helper_index import make_helper_model_from_genotype_matrix, make_helper_model_from_genotype_matrix_and_node_counts
from obgraph.genotype_matrix import GenotypeMatrix

np.random.seed(1)

logging.info("Using Python version " + platform.python_version())


def main():
    run_argument_parser(sys.argv[1:])


#def count_single_thread(reads, args):
def count_single_thread(data):
    from kage import cython_chain_genotyper

    reads, args = data
    start_time = time.time()

    read_shared_memory_name = None
    if isinstance(reads, str):
        # this is a memory address
        read_shared_memory_name = reads
        reads = from_shared_memory(SingleSharedArray, reads).array

    if len(reads) == 0:
        logging.info("Skipping thread, no more reads")
        return None, None

    reference_index = None
    if args.reference_index is not None:
        reference_index = from_shared_memory(ReferenceKmerIndex, "reference_index_shared" + args.shared_memory_unique_id)

    reference_index_scoring = None
    if args.reference_index_scoring is not None:
        reference_index_scoring = from_shared_memory(ReferenceKmerIndex, "reference_index_scoring_shared"+args.shared_memory_unique_id)

    kmer_index = from_shared_memory(KmerIndex, "kmer_index_shared"+args.shared_memory_unique_id)
    logging.info("Time spent on getting indexes from memory: %.5f" % (time.time()-start_time))

    node_counts = cython_chain_genotyper.run(reads, kmer_index, args.max_node_id, args.kmer_size,
                                                              reference_index,args.max_index_lookup_frequency, 0,
                                                              reference_index_scoring,
                                                              args.skip_chaining,
                                                              args.scale_by_frequency)
    logging.info("Time spent on getting node counts: %.5f" % (time.time()-start_time))
    shared_counts = from_shared_memory(NodeCounts, "counts_shared"+args.shared_memory_unique_id)
    shared_counts.node_counts += node_counts

    if read_shared_memory_name is not None:
        try:
            remove_shared_memory(read_shared_memory_name)
        except FileNotFoundError:
            logging.info("file not found")

    return True
    return NodeCounts(node_counts)


def count(args):
    args.shared_memory_unique_id = str(random.randint(0, 1e15))
    logging.info("Shared memory unique id: %s" % args.shared_memory_unique_id)

    truth_positions = None
    if args.truth_alignments is not None:
        from numpy_alignments import NumpyAlignments
        truth_alignments = NumpyAlignments.from_file(args.truth_alignments)
        truth_positions = truth_alignments.positions

    if args.reference_index is not None:
        reference_index = ReferenceKmerIndex.from_file(args.reference_index)
        to_shared_memory(reference_index, "reference_index_shared" + args.shared_memory_unique_id)

    if args.reference_index_scoring is not None:
        reference_index_scoring = ReferenceKmerIndex.from_file(args.reference_index_scoring)
        to_shared_memory(reference_index_scoring, "reference_index_scoring_shared" + args.shared_memory_unique_id)

    kmer_index = KmerIndex.from_file(args.kmer_index)
    to_shared_memory(kmer_index, "kmer_index_shared" + args.shared_memory_unique_id)

    max_node_id = args.max_node_id
    reads = read_chunks_from_fasta(args.reads, chunk_size=args.chunk_size, write_to_shared_memory=True)

    counts = NodeCounts(np.zeros(args.max_node_id+1, dtype=np.float))
    to_shared_memory(counts, "counts_shared" + args.shared_memory_unique_id)

    pool = Pool(args.n_threads)
    node_counts = np.zeros(max_node_id+1, dtype=float)
    for result in pool.imap(count_single_thread, zip(reads, repeat(args))):
        if result is not None:
            #logging.info("Got result. Length of counts: %d" % len(result.node_counts))
            t1 = time.time()
            #node_counts += result.node_counts
        else:
            logging.info("No results")

    #counts = NodeCounts(node_counts)
    counts = from_shared_memory(NodeCounts, "counts_shared" + args.shared_memory_unique_id)
    counts.to_file(args.node_counts_out_file_name)


def analyse_variants(args):
    from .node_count_model import NodeCountModel
    from obgraph.genotype_matrix import MostSimilarVariantLookup
    from obgraph.variant_to_nodes import VariantToNodes
    from .helper_index import CombinationMatrix

    whitelist = None

    variant_nodes = VariantToNodes.from_file(args.variant_nodes)
    kmer_index = KmerIndex.from_file(args.kmer_index)
    reverse_index = ReverseKmerIndex.from_file(args.reverse_index)
    model = NodeCountModelAdvanced.from_file(args.model)
    helper_variants = np.load(args.helper_variants)
    combination_matrix = CombinationMatrix.from_file(args.combination_matrix)
    probs = np.load(args.probs)

    analyser = GenotypeDebugger(variant_nodes, args.kmer_size, VcfVariants.from_vcf(args.vcf), kmer_index, reverse_index, VcfVariants.from_vcf(args.predicted_vcf),
                                VcfVariants.from_vcf(args.truth_vcf), TruthRegions(args.truth_regions_file), NodeCounts.from_file(args.node_counts),
                                model, helper_variants, combination_matrix, probs)
    analyser.analyse_unique_kmers_on_variants()


def genotype_single_thread(data):
    variant_interval, args = data
    min_variant_id = variant_interval[0]
    max_variant_id = variant_interval[1]
    logging.info("Starting genotyping thread from variant %d to %d" % (min_variant_id, max_variant_id))

    genotyper_class = globals()[args.genotyper]

    genotype_transition_probs = None
    if args.genotype_transition_probs is not None:
        genotype_transition_probs = from_shared_memory(GenotypeTransitionProbabilities, "genotype_transition_probs_shared" + args.shared_memory_unique_id)

    genotype_frequencies = None
    if args.genotype_frequencies is not None:
        genotype_frequencies = from_shared_memory(GenotypeFrequencies, "genotype_frequencies_shared" + args.shared_memory_unique_id)

    most_similar_variant_lookup = None
    if args.most_similar_variant_lookup is not None:
        most_similar_variant_lookup = from_shared_memory(MostSimilarVariantLookup, "most_similar_variant_lookup_shared" + args.shared_memory_unique_id)

    helper_model = None
    helper_model_combo_matrix = None

    if args.model_advanced is not None:
        model = from_shared_memory(NodeCountModelAdvanced, "model_shared" + args.shared_memory_unique_id)
    elif args.model is not None:
        logging.info("Reading model from shared memory")
        if "allele_frequencies" in args.model:
            model = from_shared_memory(NodeCountModelAlleleFrequencies, "model_shared" + args.shared_memory_unique_id)
        else:
            model = from_shared_memory(GenotypeNodeCountModel, "model_shared" + args.shared_memory_unique_id)
        logging.info(str(model))
    else:
        logging.warning("Model is None")
        model = None

    if args.helper_model is not None:
        helper_model = from_shared_memory(SingleSharedArray, "helper_model" + args.shared_memory_unique_id).array
        helper_model_combo_matrix = from_shared_memory(SingleSharedArray, "helper_model_combo_matrix" + args.shared_memory_unique_id).array

    variant_to_nodes = from_shared_memory(VariantToNodes, "variant_to_nodes_shared" + args.shared_memory_unique_id)
    node_counts = from_shared_memory(NodeCounts, "node_counts_shared" + args.shared_memory_unique_id)

    if args.tricky_variants is not None:
        tricky_variants = from_shared_memory(SingleSharedArray, "tricky_variants_shared" + args.shared_memory_unique_id).array
    else:
        tricky_variants = None

    logging.info("All indexes loaded, calling genotyper")
    genotyper = genotyper_class(model, min_variant_id, max_variant_id, variant_to_nodes, node_counts, genotype_frequencies,
                            None, avg_coverage=args.average_coverage, genotype_transition_probs=None,
                                tricky_variants=tricky_variants, use_naive_priors=args.use_naive_priors,
                                helper_model=helper_model, helper_model_combo=helper_model_combo_matrix
    )
    genotypes, probs = genotyper.genotype()
    return min_variant_id, max_variant_id, genotypes, probs


def genotype(args):
    logging.info("Using genotyper %s" % args.genotyper)
    args.shared_memory_unique_id = str(random.randint(0, 1e15))
    logging.info("Random id for shared memory: %s" % args.shared_memory_unique_id)

    if args.most_similar_variant_lookup is not None:
        most_similar_variant_lookup = MostSimilarVariantLookup.from_file(args.most_similar_variant_lookup)
        to_shared_memory(most_similar_variant_lookup, "most_similar_variant_lookup_shared" + args.shared_memory_unique_id)


    if args.model_advanced is not None:
        model = NodeCountModelAdvanced.from_file(args.model_advanced)
        to_shared_memory(model, "model_shared"+args.shared_memory_unique_id)
    else:
        try:
            model = GenotypeNodeCountModel.from_file(args.model) if args.model is not None else None
        except KeyError:
            try:
                model = NodeCountModel.from_file(args.model)
            except KeyError:
                model = NodeCountModelAlleleFrequencies.from_file(args.model)
                logging.info("Model is allele frequency model")

    variant_to_nodes = VariantToNodes.from_file(args.variant_to_nodes)
    node_counts = NodeCounts.from_file(args.counts)

    variant_store = []
    variants = VcfVariants.from_vcf(args.vcf, make_generator=True, skip_index=True)
    variant_chunks = variants.get_chunks(chunk_size=args.chunk_size, add_variants_to_list=variant_store)
    variant_chunks = ((chunk[0].vcf_line_number, chunk[-1].vcf_line_number) for chunk in variant_chunks)

    if args.tricky_variants is not None:
        logging.info("Using tricky variants")
        tricky_variants = np.load(args.tricky_variants)
        to_shared_memory(SingleSharedArray(tricky_variants), "tricky_variants_shared" + args.shared_memory_unique_id)

    if args.genotype_transition_probs is not None:
        to_shared_memory(GenotypeTransitionProbabilities.from_file(args.genotype_transition_probs), "genotype_transition_probs_shared" + args.shared_memory_unique_id)

    if args.helper_model is not None:
        helper_model = np.load(args.helper_model)
        to_shared_memory(SingleSharedArray(helper_model), "helper_model" + args.shared_memory_unique_id)
        helper_model_combo_matrix = np.load(args.helper_model_combo_matrix)
        to_shared_memory(SingleSharedArray(helper_model_combo_matrix), "helper_model_combo_matrix" + args.shared_memory_unique_id)

    if args.genotype_frequencies is not None:
        genotype_frequencies = GenotypeFrequencies.from_file(args.genotype_frequencies)
        to_shared_memory(genotype_frequencies, "genotype_frequencies_shared" + args.shared_memory_unique_id)
    if model is not None:
        to_shared_memory(model, "model_shared" + args.shared_memory_unique_id)
    to_shared_memory(variant_to_nodes, "variant_to_nodes_shared" + args.shared_memory_unique_id)
    to_shared_memory(node_counts, "node_counts_shared" + args.shared_memory_unique_id)

    pool = Pool(args.n_threads)
    results = []


    for min_variant_id, max_variant_id, genotypes, probs in pool.imap(genotype_single_thread, zip(variant_chunks, repeat(args))):
        results.append((min_variant_id, max_variant_id, genotypes, probs))
        #genotyped_variants.add_variants(result)

    i = 0
    for min_variant_id, max_variant_id, genotypes, probs in results:
        logging.info("Merging results, %d/%d" % (i, len(results)))
        i += 1
        for variant_id in range(min_variant_id, max_variant_id+1):
            variant_store[variant_id].set_genotype(genotypes[variant_id-min_variant_id], is_numeric=True)
            #variant_store[variant_id].set_filter_by_prob(probs[variant_id-min_variant_id], criteria_for_pass=args.min_genotype_quality)

    VcfVariants(variant_store, header_lines=variants.get_header(), skip_index=True).\
        to_vcf_file(args.out_file_name, ignore_homo_ref=False, add_header_lines=['##FILTER=<ID=LowQUAL,Description="Quality is low">'], sample_name_output=args.sample_name_output)
    #np.save(args.out_file_name + ".allele_frequencies", genotyper._predicted_allele_frequencies)
    #logging.info("Wrote predicted allele frequencies to %s" % args.out_file_name + ".allele_frequencies")
    np.save(args.out_file_name + ".probs", results[0][3])


def model_using_kmer_index(variant_id_interval, args):
    variant_start_id, variant_end_id = variant_id_interval
    logging.info("Processing variants with id between %d and %d" % (variant_start_id, variant_end_id))
    from .node_count_model import NodeCountModel

    allele_frequency_index = None
    if args.allele_frequency_index is not None:
        allele_frequency_index = np.load(args.allele_frequency_index)

    haplotype_matrix = None
    node_to_variants = None
    if args.haplotype_matrix is not None:
        haplotype_matrix = HaplotypeMatrix.from_file(args.haplotype_matrix)
        node_to_variants = NodeToVariants.from_file(args.node_to_variants)

    if args.version == "":
        model_class = NodeCountModelCreatorFromNoChaining
    elif args.version == "v3":
        model_class = NodeCountModelCreatorAdvanced
    elif args.version == "v2":
        model_class = NodeCountModelCreatorFromNoChainingOnlyAlleleFrequencies
        logging.warning("Using new version which gets sum of allele frequencies and squared sum")


    model_creator = model_class(
        from_shared_memory(KmerIndex, "kmer_index_shared"),
        from_shared_memory(ReverseKmerIndex, "reverse_index_shared"),
        from_shared_memory(VariantToNodes, "variant_to_nodes_shared"),
        variant_start_id, variant_end_id, args.max_node_id, scale_by_frequency=args.scale_by_frequency,
        allele_frequency_index=allele_frequency_index,
        haplotype_matrix=haplotype_matrix,
        node_to_variants=node_to_variants
    )
    model_creator.create_model()
    return model_creator.get_results()


def model_using_kmer_index_multiprocess(args):
    reverse_index = ReverseKmerIndex.from_file(args.reverse_node_kmer_index)
    to_shared_memory(reverse_index, "reverse_index_shared")
    index = KmerIndex.from_file(args.kmer_index)
    to_shared_memory(index, "kmer_index_shared")
    variant_to_nodes = VariantToNodes.from_file(args.variant_to_nodes)
    to_shared_memory(variant_to_nodes, "variant_to_nodes_shared")

    max_node_id = args.max_node_id

    logging.info("Will use %d threads" % args.n_threads)
    #variants = VcfVariants.from_vcf(args.vcf, skip_index=True, make_generator=True)
    #variants = variants.get_chunks(chunk_size=args.chunk_size)

    n_threads = args.n_threads
    n_variants = len(variant_to_nodes.ref_nodes)
    intervals = [int(i) for i in np.linspace(0, n_variants, n_threads)]
    variant_intervals = [(from_id, to_id) for from_id, to_id in zip(intervals[0:-1], intervals[1:])]
    logging.info("Will process variant intervals: %s" % variant_intervals)
    data_to_process = zip(variant_intervals, repeat(args))

    if args.version == "":
        expected_node_counts_not_following_node = np.zeros(max_node_id + 1, dtype=np.float)
        expected_node_counts_following_node = np.zeros(max_node_id + 1, dtype=np.float)
    elif args.version == "v3":
        resulting_model = NodeCountModelAdvanced.create_empty(max_node_id)
    elif args.version == "v2":
        allele_frequencies = np.zeros(max_node_id + 1, dtype=np.float)
        allele_frequencies_squared = np.zeros(max_node_id + 1, dtype=np.float)

    pool = Pool(args.n_threads)

    while True:
        results = pool.starmap(model_using_kmer_index,
                               itertools.islice(data_to_process, args.n_threads))
        if results:
            for res in results:
                if args.version == "":
                    expected_node_counts_following_node += res[0]
                    expected_node_counts_not_following_node += res[1]
                elif args.version == "v3":
                    resulting_model = resulting_model + res
                elif args.version == "v2":
                    allele_frequencies += res[0]
                    allele_frequencies_squared += res[1]
        else:
            logging.info("No results, breaking")
            break

    if args.version == "":
        model = NodeCountModel(expected_node_counts_following_node, expected_node_counts_not_following_node)
    elif args.version == "v3":
        model = resulting_model
    elif args.version == "v2":
        model = NodeCountModelAlleleFrequencies(allele_frequencies, allele_frequencies_squared)

    model.to_file(args.out_file_name)
    logging.info("Wrote model to %s" % args.out_file_name)


def run_argument_parser(args):
    parser = argparse.ArgumentParser(
        description='Alignment free graph genotyper',
        prog='alignment_free_graph_genotyper',
        formatter_class=lambda prog: argparse.HelpFormatter(prog, max_help_position=50, width=100))

    subparsers = parser.add_subparsers()
    subparser = subparsers.add_parser("count")
    subparser.add_argument("-i", "--kmer_index", required=True)
    subparser.add_argument("-r", "--reads", required=True)
    subparser.add_argument("-k", "--kmer_size", required=False, type=int, default=31)
    subparser.add_argument("-n", "--node_counts_out_file_name", required=True)
    subparser.add_argument("-M", "--max_node_id", type=int, default=2000000, required=False)
    subparser.add_argument("-t", "--n-threads", type=int, default=1, required=False)
    subparser.add_argument("-c", "--chunk-size", type=int, default=10000, required=False, help="Number of reads to process in the same chunk")
    subparser.add_argument("-T", "--truth_alignments", required=False)
    subparser.add_argument("-Q", "--reference_index", required=False)
    subparser.add_argument("-R", "--reference_index_scoring", required=False)
    subparser.add_argument("-I", "--max-index-lookup-frequency", required=False, type=int, default=5)
    subparser.add_argument("-s", "--skip-chaining", required=False, type=bool, default=False)
    subparser.add_argument("-f", "--scale-by-frequency", required=False, type=bool, default=False)
    subparser.set_defaults(func=count)



    subparser = subparsers.add_parser("analyse_variants")
    subparser.add_argument("-g", "--variant-nodes", required=True)
    subparser.add_argument("-i", "--kmer-index", required=True)
    subparser.add_argument("-k", "--kmer-size", required=True, type=int)
    subparser.add_argument("-R", "--reverse-index", required=True)
    subparser.add_argument("-v", "--vcf", required=True, help="Vcf to genotype")
    subparser.add_argument("-P", "--predicted-vcf", required=True)
    subparser.add_argument("-T", "--truth-vcf", required=True)
    subparser.add_argument("-t", "--truth-regions-file", required=True)
    subparser.add_argument("-n", "--node-counts", required=True)
    subparser.add_argument("-m", "--model", required=True)
    subparser.add_argument("-f", "--helper-variants", required=True)
    subparser.add_argument("-F", "--combination-matrix", required=True)
    subparser.add_argument("-p", "--probs", required=True)
    subparser.set_defaults(func=analyse_variants)


    subparser = subparsers.add_parser("genotype")
    subparser.add_argument("-c", "--counts", required=True)
    subparser.add_argument("-g", "--variant-to-nodes", required=True)
    subparser.add_argument("-v", "--vcf", required=True, help="Vcf to genotype")
    subparser.add_argument("-m", "--model", required=False, help="Node count model")
    subparser.add_argument("-A", "--model_advanced", required=False, help="Node count model")
    subparser.add_argument("-G", "--genotype-frequencies", required=False, help="Genotype frequencies")
    subparser.add_argument("-M", "--most_similar_variant_lookup", required=False, help="Most similar variant lookup")
    subparser.add_argument("-o", "--out-file-name", required=True, help="Will write genotyped variants to this file")
    subparser.add_argument("-C", "--genotyper", required=False, default="Genotyper", help="Genotyper to use")
    subparser.add_argument("-t", "--n-threads", type=int, required=False, default=1)
    subparser.add_argument("-z", "--chunk-size", type=int, default=100000, help="Number of variants to process in each chunk")
    subparser.add_argument("-a", "--average-coverage", type=float, default=15, help="Expected average read coverage")
    subparser.add_argument("-q", "--min-genotype-quality", type=float, default=0.95, help="Min prob of genotype being correct")
    subparser.add_argument("-p", "--genotype-transition-probs", required=False)
    subparser.add_argument("-x", "--tricky-variants", required=False)
    subparser.add_argument("-s", "--sample-name-output", required=False, default="DONOR", help="Sample name that will be used in the output vcf")
    subparser.add_argument("-u", "--use-naive-priors", required=False, type=bool, default=False, help="Set to True to use only population allele frequencies as priors.")
    subparser.add_argument("-f", "--helper-model", required=False)
    subparser.add_argument("-F", "--helper-model-combo-matrix", required=False)


    subparser.set_defaults(func=genotype)


    def run_tests(args):
        from .simulation import run_genotyper_on_simualated_data
        np.random.seed(args.random_seed)
        random.seed(args.random_seed)
        genotyper = globals()[args.genotyper]
        if args.type == "simulated":
            run_genotyper_on_simualated_data(genotyper, args.n_variants, args.n_individuals, args.average_coverage, args.coverage_std, args.duplication_rate)
        else:
            raise NotImplementedError("Not implemented")
            node_counts = NodeCounts.from_file("tests/testdata_genotyping/node_counts")
            model = GenotypeNodeCountModel.from_file("tests/testdata_genotyping/genotype_model.npz")
            variant_to_nodes = VariantToNodes.from_file("tests/testdata_genotyping/variant_to_nodes")
            genotype_frequencies = GenotypeFrequencies.from_file("tests/testdata_genotyping/genotype_frequencies")
            most_similar_variant_lookup = MostSimilarVariantLookup.from_file("tests/testdata_genotyping/most_similar_variant_lookup.npz")
            variants = VcfVariants.from_vcf("tests/testdata_genotyping/variants_no_genotypes.vcf")
            truth_variants = VcfVariants.from_vcf("tests/testdata_genotyping/truth.vcf")
            truth_regions = TruthRegions("tests/testdata_genotyping/truth_regions.bed")
            g = genotyper(model, variants, variant_to_nodes, node_counts, genotype_frequencies, most_similar_variant_lookup)
            g.genotype()

            from .analysis import SimpleRecallPrecisionAnalyser
            analyser = SimpleRecallPrecisionAnalyser(variants, truth_variants, truth_regions)
            analyser.analyse()

    subparser = subparsers.add_parser("test")
    subparser.add_argument("-g", "--genotyper", required=False, default="CombinationModelGenotyper", help="Classname of genotyper")
    subparser.add_argument("-n", "--n_variants", required=False, type=int, default=100, help="Number of variants to test on")
    subparser.add_argument("-i", "--n_individuals", required=False, type=int, default=50, help="Number of individuals")
    subparser.add_argument("-r", "--random_seed", required=False, type=int, default=1, help="Random seed")
    subparser.add_argument("-c", "--average_coverage", required=False, type=int, default=8, help="Average coverage")
    subparser.add_argument("-s", "--coverage_std", required=False, type=int, default=2, help="Coverage std")
    subparser.add_argument("-d", "--duplication_rate", required=False, type=float, default=0.1, help="Ratio of variants with duplications")
    subparser.add_argument("-T", "--type", required=False, default="simulated")
    subparser.set_defaults(func=run_tests)

    def make_genotype_model(args):
        node_counts = NodeCountModel.from_file(args.node_count_model)
        variant_nodes = VariantToNodes.from_file(args.variant_to_nodes)
        genotype_model = GenotypeNodeCountModel.from_node_count_model(node_counts, variant_nodes)
        genotype_model.to_file(args.out_file_name)

    subparser = subparsers.add_parser("make_genotype_model")
    subparser.add_argument("-n", "--node-count-model", required=True)
    subparser.add_argument("-v", "--variant-to-nodes", required=True)
    subparser.add_argument("-o", "--out-file-name", required=True)
    subparser.set_defaults(func=make_genotype_model)

    def find_tricky_variants(args):
        variant_to_nodes = VariantToNodes.from_file(args.variant_to_nodes)
        #model = GenotypeNodeCountModel.from_file(args.node_count_model)
        model = NodeCountModelAdvanced.from_file(args.node_count_model)
        reverse_index = ReverseKmerIndex.from_file(args.reverse_kmer_index)

        tricky_variants = np.zeros(len(variant_to_nodes.ref_nodes+1), dtype=np.uint32)

        n_tricky_model = 0
        n_tricky_kmers = 0
        n_nonunique = 0

        max_counts_model = args.max_counts_model

        for variant_id in range(0, len(variant_to_nodes.ref_nodes)):
            if variant_id % 100000 == 0:
                logging.info("%d variants processed, %d tricky due to model, %d tricky due to kmers. N non-unique filtered: %d" % (variant_id, n_tricky_model, n_tricky_kmers, n_nonunique))

            ref_node = variant_to_nodes.ref_nodes[variant_id]
            var_node = variant_to_nodes.var_nodes[variant_id]

            """
            model_counts_ref = sorted([
                model.counts_homo_ref[ref_node],
                model.counts_homo_alt[ref_node],
                model.counts_hetero[ref_node]
            ])
            model_counts_var = sorted([
                model.counts_homo_ref[var_node],
                model.counts_homo_alt[var_node],
                model.counts_hetero[var_node]
            ])
            """
            model_counts_ref = 1 + model.certain[ref_node] + model.frequencies[ref_node]
            model_counts_var = 1 + model.certain[var_node] + model.frequencies[var_node]

            if args.only_allow_unique:
                #if model.counts_homo_ref[var_node] > 0 or model.counts_homo_alt[ref_node] > 0:
                if model_counts_ref > 1 or model_counts_var > 1:
                    n_nonunique += 1
                    tricky_variants[variant_id] = 1

            #if model_counts_ref[2] > max_counts_model and model_counts_var[2] > max_counts_model:
            #if model_counts_ref[2] < model_counts_ref[1] * 1.1 or model_counts_var[2] < model_counts_var[1] * 1.1:
            m = args.max_counts_model
            if False and (model_counts_ref > model_counts_var * m or model_counts_var > model_counts_ref * m or model_counts_var+model_counts_ref > m*3):
                #logging.warning(model_counts_ref)
                #logging.warning(model_counts_ref)
                tricky_variants[variant_id] = 1
                n_tricky_model += 1
            else:
                reference_kmers = set(reverse_index.get_node_kmers(ref_node))
                variant_kmers = set(reverse_index.get_node_kmers(var_node))
                if len(reference_kmers.intersection(variant_kmers)) > 0:
                    #logging.warning("-----\nKmer crash on variant %d \n Ref kmers: %s\n Var kmers: %s" % (variant_id, reference_kmers, variant_kmers))
                    tricky_variants[variant_id] = 1
                    n_tricky_kmers += 1

        np.save(args.out_file_name, tricky_variants)
        logging.info("Wrote tricky variants to file %s" % args.out_file_name)

    subparser = subparsers.add_parser("find_tricky_variants")
    subparser.add_argument("-v", "--variant-to-nodes", required=True)
    subparser.add_argument("-m", "--node-count-model", required=True)
    subparser.add_argument("-r", "--reverse-kmer-index", required=True)
    subparser.add_argument("-M", "--max-counts-model", required=False, type=int, default=3, help="If model count exceeds this number, variant is tricky")
    subparser.add_argument("-o", "--out-file-name", required=True)
    subparser.add_argument("-u", "--only-allow-unique", required=False, type=bool, help="Only allow variants where all kmers are unique")
    subparser.set_defaults(func=find_tricky_variants)

    def remove_shared_memory_command_line(args):
        remove_all_shared_memory()

    subparser = subparsers.add_parser("remove_shared_memory")
    subparser.set_defaults(func=remove_shared_memory_command_line)

    def filter_variants(args):
        from obgraph.variants import VcfVariant
        f = open(args.vcf)
        n_snps_filtered = 0
        n_indels_filtered = 0
        for line in f:
            if line.startswith("#"):
                print(line.strip())
                continue

            variant = VcfVariant.from_vcf_line(line)
            if args.skip_snps and variant.type == "SNP":
                n_snps_filtered += 1
                continue

            if variant.type == "DELETION" or variant.type == "INSERTION":
                if variant.length() < args.minimum_indel_length:
                    n_indels_filtered += 1
                    continue

            print(line.strip())

        logging.info("%d snps filtered" % n_snps_filtered)
        logging.info("%d indels filtered" % n_indels_filtered)


    subparser = subparsers.add_parser("filter_variants")
    subparser.add_argument("-v", "--vcf", required=True, help="Vcf to filter")
    subparser.add_argument("-l", "--minimum-indel-length", required=False, type=int, default=0)
    subparser.add_argument("-s", "--skip-snps", required=False, type=bool, default=False)
    subparser.set_defaults(func=filter_variants)

    def analyse_kmer_index(args):

        reverse_kmers = ReverseKmerIndex.from_file(args.reverse_kmer_index)
        index = KmerIndex.from_file(args.kmer_index)
        variant_to_nodes = VariantToNodes.from_file(args.variant_to_nodes)

        from .variant_kmer_analyser import VariantKmerAnalyser
        analyser = VariantKmerAnalyser(reverse_kmers, index, variant_to_nodes, args.write_good_variants_to_file)
        analyser.analyse()


        logging.info("Done")

    # Analyse variant kmers
    subparser = subparsers.add_parser("analyse_kmer_index")
    subparser.add_argument("-r", "--reverse-kmer-index", required=True)
    subparser.add_argument("-i", "--kmer-index", required=True)
    subparser.add_argument("-g", "--variant-to-nodes", required=True)
    subparser.add_argument("-o", "--write-good-variants-to-file", required=False, help="When specified, good variant IDs will be written to file")
    subparser.set_defaults(func=analyse_kmer_index)


    subparser = subparsers.add_parser("model")
    subparser.add_argument("-g", "--variant-to-nodes", required=True)
    subparser.add_argument("-N", "--node-to-variants", required=False)
    subparser.add_argument("-H", "--haplotype-matrix", required=False)
    subparser.add_argument("-k", "--kmer-size", required=True, type=int)
    subparser.add_argument("-i", "--kmer-index", required=True)
    subparser.add_argument("-o", "--out-file-name", required=True)
    subparser.add_argument("-m", "--max-node-id", type=int, required=True)
    subparser.add_argument("-r", "--reverse_node_kmer_index", required=True)
    #subparser.add_argument("-v", "--vcf", required=True)
    #subparser.add_argument("-c", "--chunk-size", type=int, default=100000, help="Number of variants to process in each chunk")
    subparser.add_argument("-t", "--n-threads", type=int, default=1, required=False)
    subparser.add_argument("-f", "--scale-by-frequency", required=False, type=bool, default=False)
    subparser.add_argument("-a", "--allele-frequency-index", required=False)
    subparser.add_argument("-V", "--version", required=False, default="v3")
    subparser.set_defaults(func=model_using_kmer_index_multiprocess)

    def model_using_transition_probs(args):
        from .node_count_model import GenotypeModelCreatorFromTransitionProbabilities
        from obgraph.variant_to_nodes import NodeToVariants
        graph = ObGraph.from_file(args.graph)
        genotype_matrix = GenotypeMatrix.from_file(args.genotype_matrix)
        variant_to_nodes = VariantToNodes.from_file(args.variant_to_nodes)
        node_to_variants = NodeToVariants.from_file(args.node_to_variants)
        mapping_index = KmerIndex.from_file(args.mapping_index)
        population_kemrs = KmerIndex.from_file(args.population_kmers)

        maker = GenotypeModelCreatorFromTransitionProbabilities(graph, genotype_matrix, variant_to_nodes, node_to_variants, mapping_index, population_kemrs,
                                                                args.max_node_id)

        maker.get_node_counts()
        genotype_model = GenotypeNodeCountModel(maker.counts_homo_ref, maker.counts_homo_alt, maker.counts_hetero)
        genotype_model.to_file(args.out_file_name)

    subparser = subparsers.add_parser("model_using_kmer_index2")
    subparser.add_argument("-g", "--graph", required=True)
    subparser.add_argument("-G", "--genotype_matrix", required=True)
    subparser.add_argument("-v", "--variant-to-nodes", required=True)
    subparser.add_argument("-V", "--node_to_variants", required=True)
    subparser.add_argument("-i", "--mapping-index", required=True)
    subparser.add_argument("-I", "--population-kmers", required=True)
    subparser.add_argument("-o", "--out-file-name", required=True)
    subparser.add_argument("-m", "--max-node-id", type=int, required=True)
    subparser.add_argument("-t", "--n-threads", type=int, default=1, required=False)
    subparser.set_defaults(func=model_using_transition_probs)

    def filter_vcf(args):
        from .variant_filtering import remove_overlapping_indels
        remove_overlapping_indels(args.vcf_file_name)

    subparser = subparsers.add_parser("remove_overlapping_indels")
    subparser.add_argument("-v", "--vcf-file-name", required=True)
    subparser.set_defaults(func=filter_vcf)


    def create_helper_model_single_thread(data):
        interval, args = data
        from_variant, to_variant = interval
        model = from_shared_memory(NodeCountModelAdvanced, "model"+args.shared_memory_unique_id)
        variant_to_nodes = from_shared_memory(VariantToNodes, "variant_to_nodes"+args.shared_memory_unique_id)
        genotype_matrix = from_shared_memory(GenotypeMatrix, "genotype_matrix"+args.shared_memory_unique_id)

        # read genotype matrix etc from shared memory
        submatrix = GenotypeMatrix(genotype_matrix.matrix[from_variant:to_variant,:])
        sub_variant_to_nodes = variant_to_nodes.slice(from_variant, to_variant)
        use_duplicate_counts = args.use_duplicate_counts
        if use_duplicate_counts:
            logging.info("Making helper using duplicate count info")
            subhelpers, subcombo = make_helper_model_from_genotype_matrix_and_node_counts(submatrix,
                                                                                      model,
                                                                                      sub_variant_to_nodes,
                                                                                      args.window_size)
        else:
            logging.info("Making helper without using duplicate count info")
            subhelpers, subcombo = make_helper_model_from_genotype_matrix(submatrix.matrix, None, dummy_count=1.0, window_size=args.window_size)

        # variant ids in results are now from 0 to (to_variant-from_variant)
        subhelpers += from_variant
        return from_variant, to_variant, subhelpers, subcombo

    def create_helper_model(args):
        args.shared_memory_unique_id = str(random.randint(0, 1e15))

        model = NodeCountModelAdvanced.from_file(args.node_count_model)
        variant_to_nodes = VariantToNodes.from_file(args.variant_to_nodes)
        genotype_matrix = GenotypeMatrix.from_file(args.genotype_matrix)
        # NB: Transpose
        genotype_matrix.matrix = genotype_matrix.matrix.transpose()

        logging.info("Genotype matrix shape: %s" % str(genotype_matrix.matrix.shape))
        # convert to format used in helper code
        #genotype_matrix = genotype_matrix.convert_to_other_format()
        logging.info("Genotype matrix shape after conversion: %s" % str(genotype_matrix.matrix.shape))
        most_similar = None
        if args.most_similar_variants is not None:
            most_similar = MostSimilarVariantLookup.from_file(args.most_similar_variants)

        if args.n_threads > 1:
            n_threads = args.n_threads
            n_variants = len(variant_to_nodes.ref_nodes)
            intervals = [int(i) for i in np.linspace(0, n_variants, n_threads)]
            variant_intervals = [(from_id, to_id) for from_id, to_id in zip(intervals[0:-1], intervals[1:])]
            logging.info("Will process variant intervals: %s" % variant_intervals)

            helpers = np.zeros(n_variants, dtype=np.uint32)
            genotype_matrix_combo = np.zeros((n_variants, 3, 3), dtype=np.float)

            # put data in shared memory
            to_shared_memory(genotype_matrix, "genotype_matrix"+args.shared_memory_unique_id)
            to_shared_memory(variant_to_nodes, "variant_to_nodes"+args.shared_memory_unique_id)
            to_shared_memory(model, "model"+args.shared_memory_unique_id)

            pool = Pool(args.n_threads)
            # genotyped_variants = VcfVariants(header_lines=variants.get_header())

            for from_variant, to_variant, subhelpers, subcombo in pool.imap(create_helper_model_single_thread,
                                                                              zip(variant_intervals, repeat(args))):
                helpers[from_variant:to_variant] = subhelpers
                genotype_matrix_combo[from_variant:to_variant] = subcombo

        else:
            helpers, genotype_matrix_combo = make_helper_model_from_genotype_matrix_and_node_counts(genotype_matrix,
                                                                                                model,
                                                                                                variant_to_nodes,
                                                                                                args.window_size)

        np.save(args.out_file_name, helpers)
        logging.info("Saved helper model to file: %s" % args.out_file_name)
        np.save(args.out_file_name + "_combo_matrix", genotype_matrix_combo)
        logging.info("Saved combo matrix to file %s" % args.out_file_name+"_combo_matrix")


    subparser = subparsers.add_parser("create_helper_model")
    subparser.add_argument("-g", "--genotype-matrix", required=False)
    subparser.add_argument("-n", "--node-count-model", required=True)
    subparser.add_argument("-v", "--variant-to-nodes", required=True)
    subparser.add_argument("-m", "--most-similar-variants", required=False)
    subparser.add_argument("-o", "--out-file-name", required=True)
    subparser.add_argument("-t", "--n-threads", required=False, default=1, type=int)
    subparser.add_argument("-u", "--use-duplicate-counts", required=False, type=bool, default=False)
    subparser.add_argument("-w", "--window-size", required=False, default=50, type=int,
                           help="Number of variants before/after considered as potential helper variant")
    subparser.set_defaults(func=create_helper_model)


    if len(args) == 0:
        parser.print_help()
        sys.exit(1)

    args = parser.parse_args(args)
    args.func(args)
    remove_shared_memory_in_session()

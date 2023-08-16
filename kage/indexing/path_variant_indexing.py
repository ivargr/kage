import logging
import time
import npstructures as nps
import numpy as np
import bionumpy as bnp
from graph_kmer_index import KmerIndex
import tqdm
from kage.indexing.graph import Graph
from kage.indexing.kmer_scoring import FastApproxCounter
from kage.indexing.signatures import Signatures

from .paths import Paths
from .sparse_haplotype_matrix import SparseHaplotypeMatrix
from ..models.mapping_model import LimitedFrequencySamplingComboModel
from .tricky_variants import TrickyVariants

"""
Module for simple variant signature finding by using static predetermined paths through the "graph".

Works when all variants are biallelic and there are no overlapping variants.
"""


class SignaturesWithNodes:
    """
    Represents signatures compatible with graph-nodes.
    Nodes are given implicitly from variant ids. Makes it possible
    to create a kmer index that requires nodes.
    """
    pass


class MappingModelCreator:
    def __init__(self, graph: Graph, kmer_index: KmerIndex,
                 haplotype_matrix: SparseHaplotypeMatrix,
                 max_count=10, k=31):
        self._graph = graph
        self._kmer_index = kmer_index
        self._haplotype_matrix = haplotype_matrix
        self._n_nodes = graph.n_nodes()
        self._counts = LimitedFrequencySamplingComboModel.create_empty(self._n_nodes, max_count)
        logging.info("Inited empty model")
        self._k = k
        self._max_count = max_count

    def _process_individual(self, i):
        logging.info("Processing individual %d" % i)
        t0 = time.perf_counter()
        # extract kmers from both haplotypes and map these using the kmer index
        haplotype1 = self._haplotype_matrix.get_haplotype(i*2)
        haplotype2 = self._haplotype_matrix.get_haplotype(i*2+1)
        logging.info("Got haplotypes, took %.2f seconds" % (time.perf_counter() - t0))

        haplotype1_nodes = self._haplotype_matrix.get_haplotype_nodes(i*2)
        haplotype2_nodes = self._haplotype_matrix.get_haplotype_nodes(i*2+1)

        sequence1 = self._graph.sequence(haplotype1).ravel()
        sequence2 = self._graph.sequence(haplotype2).ravel()
        logging.info("Got haplotypes, total now: %.2f seconds" % (time.perf_counter() - t0))

        kmers1 = bnp.get_kmers(sequence1, self._k).ravel().raw().astype(np.uint64)
        kmers2 = bnp.get_kmers(sequence2, self._k).ravel().raw().astype(np.uint64)
        logging.info("Got kmers, total now: %.2f seconds" % (time.perf_counter() - t0))

        t0_mapping_kmers = time.perf_counter()
        node_counts = self._kmer_index.map_kmers(kmers1, self._n_nodes)
        node_counts += self._kmer_index.map_kmers(kmers2, self._n_nodes)
        logging.info("Mapped kmers, took: %.2f seconds" % (time.perf_counter() - t0_mapping_kmers))

        self._add_node_counts(haplotype1_nodes, haplotype2_nodes, node_counts)
        logging.info("Done, total now: %.2f seconds" % (time.perf_counter() - t0))

    def _add_node_counts(self, haplotype1_nodes, haplotype2_nodes, node_counts):
        # split into nodes that the haplotype has and nodes not
        # mask represents the number of haplotypes this individual has per node (0, 1 or 2 for diploid individuals)
        mask = np.zeros(self._n_nodes, dtype=np.int8)
        mask[haplotype1_nodes] += 1
        mask[haplotype2_nodes] += 1
        for genotype in [0, 1, 2]:
            nodes_with_genotype = np.where(mask == genotype)[0]
            counts_on_nodes = node_counts[nodes_with_genotype].astype(int)
            below_max_count = np.where(counts_on_nodes < self._max_count)[
                0]  # ignoring counts larger than supported by matrix
            self._counts.diplotype_counts[genotype][
                nodes_with_genotype[below_max_count], counts_on_nodes[below_max_count]
            ] += 1

    def run(self) -> LimitedFrequencySamplingComboModel:
        n_variants, n_haplotypes = self._haplotype_matrix.shape
        n_nodes = n_variants * 2
        for individual in tqdm.tqdm(range(n_haplotypes // 2), total=n_haplotypes // 2, desc="Creating count model", unit="individuals"):
            self._process_individual(individual)

        return self._counts




def make_linear_reference_kmer_counter(reference_file_name, k=31, modulo=20000033):
    """
    Gets all kmers from a linear reference.
    """
    from graph_kmer_index.kmer_counter import KmerCounter
    logging.info("Getting kmers from linear ref")
    reference = bnp.open(reference_file_name).read()
    reference.sequence[reference.sequence == "N"] = "A"  # tmp hack to allow kmer encoding

    kmers = bnp.get_kmers(reference.sequence, k)
    kmers = kmers.raw().ravel().astype(np.uint64)
    
    from graph_kmer_index.kmer_hashing import kmer_hashes_to_reverse_complement_hash
    logging.info("Getting reverse complements")
    revcomp = kmer_hashes_to_reverse_complement_hash(kmers, k)
    kmers = np.concatenate([kmers, revcomp])

    counter = KmerCounter.from_kmers(kmers, modulo)
    return counter



def make_scorer_from_paths(paths: Paths, k, modulo):
    kmers = []
    for path in tqdm.tqdm(paths.paths, desc="Getting kmers from paths", unit="paths"):
        kmers.append(bnp.get_kmers(path.ravel(), k=k).raw().ravel().astype(np.uint64))
        kmers.append(bnp.get_kmers(bnp.sequence.get_reverse_complement(path).ravel(), k=k).raw().ravel().astype(np.uint64))

    kmers = np.concatenate(kmers).astype(np.int64)

    # using fastapproccounter, we only need to bincount
    counter = np.bincount(kmers % modulo, minlength=modulo)
    return FastApproxCounter(counter, modulo=modulo)

    #logging.info("Getting unique kmers and counts")
    #unique, counts = np.unique(kmers, return_counts=True)
    #return FastApproxCounter.from_keys_and_values(unique, counts, modulo=modulo)


def find_tricky_variants_from_signatures(signatures: Signatures):
    """
    Finds variants with bad signatures.
    """
    nonunique_ref = np.in1d(signatures.ref.ravel(), signatures.alt.ravel())
    nonunique_alt = np.in1d(signatures.alt.ravel(), signatures.ref.ravel())
    #mask_ref = np.zeros_like(signatures.ref, dtype=bool)
    #mask_ref[nonunique_ref] = True
    mask_ref = nps.RaggedArray(nonunique_ref, signatures.ref.shape)
    mask_alt = nps.RaggedArray(nonunique_alt, signatures.alt.shape)
    print(nonunique_ref, nonunique_alt)
    #mask_alt = np.zeros_like(signatures.alt, dtype=bool)
    #mask_alt[nonunique_alt] = True

    tricky_variants_ref = np.any(mask_ref, axis=1)
    tricky_variants_alt = np.any(mask_alt, axis=1)
    tricky_variants = np.logical_or(tricky_variants_ref, tricky_variants_alt)

    logging.info(f"{np.sum(tricky_variants)} tricky variants")
    return TrickyVariants(tricky_variants)


def find_tricky_variants_from_signatures2(signatures: Signatures):
    n_tricky_no_signatures = 0
    tricky_variants = np.zeros(signatures.ref.shape[0], dtype=bool)
    for i, (ref, alt) in tqdm.tqdm(enumerate(zip(signatures.ref, signatures.alt)), total=signatures.ref.shape[0], desc="Finding tricky variants", unit="variants"):
        if len(ref) == 0 or len(alt) == 0:
            tricky_variants[i] = True
            n_tricky_no_signatures += 1
        if set(ref).intersection(alt):
            tricky_variants[i] = True

    logging.info(f"{np.sum(tricky_variants)} tricky variants")
    logging.info(f"Tricky because no signatures: {n_tricky_no_signatures}")
    return TrickyVariants(tricky_variants)


def find_tricky_variants_with_count_model(signatures: Signatures, model):
    tricky = find_tricky_variants_from_signatures2(signatures).tricky_variants

    # also check if model is missing data
    n_missing_model = 0
    for variant in tqdm.tqdm(range(len(signatures.ref.shape[1])), total=signatures.ref.shape[0], desc="Finding tricky variants", unit="variants"):
        ref_node = variant*2
        alt_node = variant*2 + 1
        if model.has_no_data(ref_node, threshold=3) or model.has_no_data(alt_node, threshold=3):
            tricky[variant] = True
            n_missing_model += 1
            print(model.describe_node(ref_node))
            print(model.describe_node(alt_node))

    logging.info("N tricky variants due to missing data in model: %d" % n_missing_model)
    return TrickyVariants(tricky)




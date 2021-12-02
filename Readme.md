
## kage: *K*mer-based *A*lignment-free *G*raph G*e*notyper
KAGE is a tool for efficiently genotyping short SNPs and indels from short genomic reads.


## Installation
Kage required Python 3, and can be installed using Pip: 
```
pip install kage-genotyper
```

Test that the installation worked:

```bash
kage test 
```

The above will perform genotyping on some dummy data and should finish without any errors. 


## How to run
**kage** is easy and fast to use once you have indexes built for the variants you want to genotype. However, building these indexes can take some time. Therefore, we have prebuilt indexes for 100 Genomes Projects variants, which can be [downloaded from here](..).

If you want to make your own indexes for your own reference genome and variants, you should use the kage Snakemake pipeline which can [be found here](https://github.com/ivargr/genotyping-benchmarking). Feel free to contact us if you want help making these indexes for your desired variants.

Once you have an index of the variants you want to genotype, running kage is straight-forward:

### Step 1: Map fasta kmers to the pangenome index:
```python
kmer_mapper -b index -f reads.fa -o kmer_counts -l 150
```

Note: Make sure l is the max read length of your input reads, and not any lower than tatt. The index specified by `-b` is an index bundle (see explanation above).


### Step 2: Do the genotyping
Count kmers:
```bash
kage genotype -i index -c kmer_counts --average-coverage 15 -o genotypes.vcf
```

Make sure to set `--average-coverage` to the expected average coverage of your input reads. The resulting predicted genotypes will be written to the file specified by `-o`.
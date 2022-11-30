# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/01_filter.ipynb.

# %% auto 0
__all__ = ['logger', 'MINHASH_SEED', 'NON_ALPHA', 'lsh', 'dup_ids', 'check_char_repetition', 'check_perplexity', 'check_language',
           'minhash_dedup']

# %% ../nbs/01_filter.ipynb 2
import datasets
import gc
import logging
import multiprocessing
import os
import random
import re

import networkit as nk
import numpy as np

from collections import Counter
from datasets import Dataset, Features, Value, Sequence
from datasketch import LeanMinHash, MinHash, MinHashLSH
from rich.logging import RichHandler
from .helpers import flagged_words, get_words
from tqdm.auto import tqdm

# %% ../nbs/01_filter.ipynb 3
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(RichHandler(rich_tracebacks=True))
logger.propagate = False
datasets.logging.set_verbosity_error()
# Turn off logging for datasets
logging.getLogger("datasets").setLevel(logging.ERROR)

# %% ../nbs/01_filter.ipynb 5
def _char_rep_ratio(
    doc: str, # document to be analyzed
    char_rep_len: int, # length of character repetition
) -> float:
    """
    Returns the ratio of character repetitions in a document.
    """
    def calc_ngrams(doc, n):
        char_ngrams = [
            doc[i : i + n] for i in range(len(doc) - n + 1)
        ]
        freq_char_ngrams = Counter(char_ngrams)
        return freq_char_ngrams

    freq_char_ngrams = calc_ngrams(
        doc, char_rep_len
    )
    if len(freq_char_ngrams) == 0:
        return 0
    freq_char_ngrams = list(freq_char_ngrams.values())
    freq_char_ngrams = sorted(freq_char_ngrams, reverse=True)
    val_one = len([el for el in freq_char_ngrams if el == 1])
    num_rep_char_ngrams = min(
        int(np.sqrt(len(freq_char_ngrams))),
        len(freq_char_ngrams) - val_one,
    )
    char_rep_ratio = sum(
        freq_char_ngrams[:num_rep_char_ngrams]
    ) / sum(freq_char_ngrams)
    return char_rep_ratio

# %% ../nbs/01_filter.ipynb 6
def check_char_repetition(
    document,                       # document to be analyzed
    char_repetition_len=10,         # length of character repetition
    char_repetition_threshold=0.2,  # threshold for character repetition
    dry_run=False,                  # if True, returns the ratio of character repetition
) -> bool: # returns True if document is below threshold
    """
    Checks if the document is below the character repetition threshold.
    """
    char_rep_ratio = _char_rep_ratio(
        document, char_repetition_len
    )
    if dry_run:
        return char_rep_ratio
    else:
        return char_rep_ratio <= char_repetition_threshold

# %% ../nbs/01_filter.ipynb 8
def _flag_word_ratio(
    doc: str, # document to be analyzed
    flagged_words: list, # list of flagged words
    get_words_func: callable, # function to get words from document
) -> float: # returns ratio of flagged words in document
    """
    Returns the ratio of flagged words in a document.
    """
    words = get_words_func(doc)
    if not words:
        return 0.
    flagged_words_ratio = len(
        [word for word in words if word in flagged_words]
    ) / len(words)
    if flagged_words_ratio > 1.0:
        flagged_words_ratio = 1.0
    return flagged_words_ratio

# %% ../nbs/01_filter.ipynb 9
def check_flagged_words(
    document: str,                              # document to be analyzed
    flagged_words: list = flagged_words["en"],  # list of flagged words
    flagged_words_threshold: float = 0.1,       # threshold for flagged words
    get_words_func: callable = get_words,       # function to get words from document
    dry_run: bool = False,                      # if True, returns the ratio of flagged words
) -> bool:                                      # returns True if document is below threshold unless dry_run is True
    """
    Checks if a document contains a high percentage of flagged words.
    """
    cond = True
    if flagged_words:
        flagged_words_ratio = _flag_word_ratio(
            document,
            flagged_words,
            get_words_func,
        )
        if dry_run:
            return flagged_words_ratio

        cond = flagged_words_ratio <= flagged_words_threshold
    return cond

# %% ../nbs/01_filter.ipynb 12
def check_perplexity(
    document, # document to be analyzed
    perplexity_threshold=10_000, # threshold for perplexity
    model=None, # model to calculate perplexity
    dry_run=False, # if True, returns the perplexity of the document
) -> bool: # returns True if document is below threshold
    """
    Checks if the document is below the perplexity threshold.
    """
    perplexity = model.get_perplexity(document)
    if dry_run:
        return perplexity
    else:
        return perplexity <= perplexity_threshold

# %% ../nbs/01_filter.ipynb 15
def check_language(
    document, # document to be analyzed
    language="en", # language to check
    language_threshold=0.9, # threshold for language
    model=None, # model to check language
    dry_run=False, # if True, returns the language of the document
) -> bool: # returns True if document is below threshold
    """
    Checks if the document is below the language threshold.
    """
    lang, prob = model.get_language(document)
    if dry_run:
        return lang, prob
    else:
        return language == lang and prob > language_threshold

# %% ../nbs/01_filter.ipynb 19
multiprocessing.set_start_method("fork", force=True)

MINHASH_SEED = 115
NON_ALPHA = re.compile("[^A-Za-z_0-9]")

random.seed(MINHASH_SEED)

lsh: MinHashLSH = None
dup_ids: set[int] = None

# %% ../nbs/01_filter.ipynb 20
def _hash_func(
    idx: int, # The index of the record.
    content: str, # The content to be hashed.
    *,
    num_perm: int # The number of permutations to use in the MinHash object.
) -> dict[str, any]: # The MinHash signature and the index of the record.
    """
    Embed the content of a record into a MinHash object. This function should be
    used with multiprocessing and it scales well with the number of cores.
    >>> result = _hash_func(0, "Hello world!", num_perm=128)
    >>> result["__id__"]
    0
    >>> result["__signature__"].shape
    (128,)
    >>> result["__signature__"].dtype
    dtype('uint64')
    """
    m = MinHash(num_perm=num_perm, seed=MINHASH_SEED)
    m.update_batch([token.encode("utf-8") for token in {t for t in NON_ALPHA.split(content) if t}])
    return {"__signature__": m.hashvalues, "__id__": idx}

# %% ../nbs/01_filter.ipynb 22
def _query_content(
    idx: int, # The index of the record.
    signature: np.ndarray, # The MinHash signature of the record to be queried.
    *,
    index: MinHashLSH # The MinHashLSH index. It is shared across all processes when using multiprocessing with fork without copy.
) -> dict[str, any]: # The query result.
    """
    Query the MinHashLSH index for the record. This function can be used with multiprocessing
    as long as the index is shared across processes.
    """
    return {
        "__neighbors__": [
            dup_idx
            for dup_idx in index.query(
                LeanMinHash(seed=MINHASH_SEED, hashvalues=signature),
            )
            if dup_idx != idx  # exclude itself
        ],
        "__id__": idx,
    }

# %% ../nbs/01_filter.ipynb 24
def _jaccard_similarity(
    s1: str, # The first string to compare.
    s2: str # The second string to compare.
) -> float: # The Jaccard similarity between the two strings.
    """
    Calculate the jaccard similarity between two code snippets.
    """
    tokens1 = set([t for t in NON_ALPHA.split(s1) if t.strip()])
    tokens2 = set([t for t in NON_ALPHA.split(s2) if t.strip()])
    return len(tokens1 & tokens2) / max(1, len(tokens1 | tokens2))

# %% ../nbs/01_filter.ipynb 26
def _calculate_average_false_positive_rate(
    clusters: list[list[int]], # The clusters of duplicate records.
    reference_records: Dataset, # The reference records.
    threshold: float, # The threshold to use for calculating the false positive rate.
    column: str, # The column to use for calculating the false positive rate.
) -> None:
    """
    Calculate the average false positive rate within each cluster. The false positives are defined as
    number of examples that have a maximum jaccard similarity with any example in the cluster that is
    less than the threshold. The false positive rate is defined as the number of false positives divided
    by the number of examples in the cluster. The average false positive rate is defined as the average
    of the false positive rate across all clusters given.
    """
    cluster_false_positive_rates: list[float] = []
    deltas: list[float] = []

    for cluster in tqdm(clusters, desc="Calculating sampling false positive rate..."):
        num_false_positives = 0
        ids = sorted(cluster)
        for i, x in enumerate(ids):
            is_false_positive = True
            max_similarity = -float("inf")
            for j, y in enumerate(ids):
                if i == j:
                    continue
                # TODO This can be redundant but we only calculate this for a small sample
                similarity = _jaccard_similarity(reference_records[x][column], reference_records[y][column])
                max_similarity = max(max_similarity, similarity)
                if max_similarity >= threshold:
                    is_false_positive = False
                    break
            if is_false_positive:
                num_false_positives += 1
                deltas.append(threshold - max_similarity)
        cluster_false_positive_rates.append(num_false_positives / len(ids))

    logger.info(
        f"Average false positive rate from {len(clusters)} clusters: {np.mean(cluster_false_positive_rates):.2f}"
    )
    logger.info(f"Similarity delta stats from threshold:")
    logger.info(f"-  Max : {np.max(deltas):0.2f}")
    logger.info(f"-  Min : {np.min(deltas):0.2f}")
    logger.info(f"-  Mean: {np.mean(deltas):0.2f}")
    logger.info(f"-  Std : {np.std(deltas):0.2f}")

# %% ../nbs/01_filter.ipynb 27
def _find_duplicate_communities(
    records: Dataset, # The dataset that contains both `__id__` and `__neighbors__`.
    community_detection: bool, # Whether to use community detection to find the duplicate communities, or to use the connected components.
    report_false_positive_rate: bool = False, # Whether to report the false positive rate.
    reference_records: Dataset = None, # The reference records. It can be an iterable or a Dataset. It is only used when `report_false_positive_rate` is True.
    threshold: float = 0.85, # The threshold to use for calculating the false positive rate.
    column: str = "content", # The column to use for calculating the false positive rate.
    verbose: bool = False,
) -> set[int]: # The set of duplicate ids that should be removed, leaving only one id in each community.
    """
    Find the duplicate communities from the queried dataset.
    """
    SAMPLE_MIN_SIZE = 10
    SAMPLE_MAX_SIZE = 100
    SAMPLE_SIZE = 10
    g = nk.graph.Graph()
    for record in tqdm(records, desc="Constructing graph..."):
        for y in record["__neighbors__"]:
            g.addEdge(record["__id__"], y, addMissing=True)

    to_remove: set[int] = set()
    samples: list[list[int]] = []
    if not community_detection:
        cc = nk.components.ConnectedComponents(g)
        cc.run()
        partition = cc.getPartition()
        components = list(cc.getComponents())
        random.shuffle(components)
        for component in tqdm(components, desc="Iterating over components..."):
            component = sorted(component)
            to_remove.update(component[1:])
            if len(samples) < SAMPLE_SIZE and SAMPLE_MAX_SIZE > len(component) >= SAMPLE_MIN_SIZE:
                samples.append(component[:])
    else:
        algo = nk.community.PLM(g, refine=False)
        algo.run()
        partition = algo.getPartition()
        communities = list(partition.getSubsetIds())
        random.shuffle(communities)
        # This can be slow if there are many communities
        for i in tqdm(communities, desc="Iterating over communities..."):
            ids = partition.getMembers(i)
            to_remove.update(sorted(ids)[1:])
            if len(samples) < SAMPLE_SIZE and SAMPLE_MAX_SIZE > len(ids) >= SAMPLE_MIN_SIZE:
                samples.append(ids)

    if report_false_positive_rate and verbose:
        _calculate_average_false_positive_rate(
            samples,
            reference_records,
            threshold,
            column,
        )

    return to_remove

# %% ../nbs/01_filter.ipynb 28
def minhash_dedup(
    ds,                                         # The dataset to deduplicate.
    column,                                     # The column to use for deduplication.
    community_detection: bool = False,          # Whether to use community detection to find the duplicate communities, or to use the connected components.
    report_false_positive_rate: bool = False,   # Whether to report the false positive rate.
    threshold: float = 0.85,                    # The threshold to use for deduplication.
    num_perm: int = 128,                        # The number of permutations to use for minhashing.
    dry_run: bool = False,                      # Whether to run the deduplication in dry run mode.
) -> Dataset:
    """
    Deduplicate the dataset using minhashing as described in the paper "Deduplicating Training Data Makes Language Models Better".
    """
    global lsh
    global dup_ids

    lsh = MinHashLSH(
        threshold=threshold,
        num_perm=num_perm,
    )
    column_names = ds.column_names
    ds = ds.map(
        lambda _, idx: {"__id__": idx},
        with_indices=True,
        num_proc=os.cpu_count(),
        desc="Adding index...",
    )
    hashed_ds = ds.map(
        function=_hash_func,
        fn_kwargs={"num_perm": num_perm},
        input_columns=["__id__", column],
        remove_columns=column_names,
        num_proc=os.cpu_count(),
        desc=f"Fingerprinting...",
    )
    with lsh.insertion_session() as session:
        for data in tqdm(hashed_ds, desc="Indexing signatures..."):
            if data["__id__"] in lsh:
                continue
            session.insert(
                data["__id__"],
                LeanMinHash(seed=MINHASH_SEED, hashvalues=data["__signature__"]),
                check_duplication=False,
            )
    
    gc.disable()
    gc.freeze()

    conf = {
        "threshold": threshold,
        "community_detection": community_detection,
        "report_false_positive_rate": report_false_positive_rate,
        "num_perm": num_perm,
        "name": ds.builder_name,
        "column": column,
    }
    queried = hashed_ds.map(
        lambda x, y: _query_content(x, y, index=lsh),
        num_proc=os.cpu_count(),
        features=Features({
            "__id__": Value(dtype='int64', id=None),
            "__neighbors__": Sequence(feature=Value(dtype='int64', id=None), length=-1, id=None)
        }),
        input_columns=["__id__", "__signature__"],
        remove_columns=["__signature__"],
        desc=f"Querying...",
    )

    del lsh
    gc.collect()

    queried = queried.filter(
        lambda x: len(x["__neighbors__"]) > 0,
        num_proc=os.cpu_count(),
        desc="Finding duplicates..."
    )
    dup_ids = _find_duplicate_communities(
        records=queried,
        community_detection=conf["community_detection"],
        report_false_positive_rate=conf["report_false_positive_rate"],
        reference_records=ds,
        threshold=conf["threshold"],
        column=conf["column"],
    )

    del queried
    gc.collect()

    if dry_run:
        final_data = ds.map(
            lambda idx: {"duplicate": idx in dup_ids},
            input_columns=["__id__"],
            num_proc=os.cpu_count(),
            desc="Labeling duplicates...",
        )
    else:
        final_data = ds.filter(
            lambda idx: idx not in dup_ids,
            input_columns=["__id__"],
            num_proc=os.cpu_count(),
            desc="Filtering duplicates...",
        )
    return final_data

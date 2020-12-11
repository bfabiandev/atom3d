import math
import random

import numpy as np

import atom3d.protein.sequence as seq
import atom3d.util.file as fi
import atom3d.util.log as log

logger = log.get_logger('sequence_splits')


####################################
# split by pre-clustered sequence
# identity clusters from PDB
####################################


def cluster_split(all_chain_sequences, cutoff, val_split=0.1,
                  test_split=0.1, min_fam_in_split=5, random_seed=None):
    """
    Splits pdb dataset using pre-computed sequence identity clusters from PDB.

    Generates train, val, test sets.

    Args:
        all_chain_sequences ((str, chain_sequences)[]):
            tuple of pdb ids and chain_sequences in dataset
        cutoff (float):
            sequence identity cutoff (can be .3, .4, .5, .7, .9, .95, 1.0)
        val_split (float): fraction of data used for validation. Default: 0.1
        test_split (float): fraction of data used for testing. Default: 0.1
        min_fam_in_split (int): controls variety of val/test sets. Default: 5
        random_seed (int):  specifies random seed for shuffling. Default: None

    Returns:
        train_set (str[]):  pdbs in the train set
        val_set (str[]):  pdbs in the validation set
        test_set (str[]): pdbs in the test set

    """
    if random_seed is not None:
        np.random.seed(random_seed)

    pdb_codes = \
        np.unique([fi.get_pdb_code(x[0]) for (x, _) in all_chain_sequences])
    n_orig = len(pdb_codes)
    clusterings = seq.get_pdb_clusters(cutoff, pdb_codes)

    # If code not present in clustering, we don't use.
    all_chain_sequences = \
        [(x, cs) for (x, cs) in all_chain_sequences
         if fi.get_pdb_code(x[0]) in clusterings[0]]
    pdb_codes = \
        np.unique([fi.get_pdb_code(x[0]) for (x, _) in all_chain_sequences])
    n = len(pdb_codes)

    logger.info(
        f'Removing {n_orig - n:} / {n_orig:} '
        f'sequences due to not finding in clustering.')

    test_size = n * test_split
    val_size = n * val_split

    np.random.shuffle(all_chain_sequences)

    logger.info('generating validation set...')
    val_set, all_chain_sequences = create_cluster_split(
        all_chain_sequences, clusterings, val_size, min_fam_in_split)
    logger.info('generating test set...')
    test_set, all_chain_sequences = create_cluster_split(
        all_chain_sequences, clusterings, test_size, min_fam_in_split)
    train_set = all_chain_sequences

    train_set = [x[0] for x in train_set]
    val_set = [x[0] for x in val_set]
    test_set = [x[0] for x in test_set]

    logger.info(f'train size {len(train_set):}')
    logger.info(f'val size {len(val_set):}')
    logger.info(f'test size {len(test_set):}')

    return train_set, val_set, test_set


def create_cluster_split(all_chain_sequences, clusterings, split_size, min_fam_in_split):
    """
    Create a split while retaining diversity specified by min_fam_in_split.
    Returns split and removes any pdbs in this split from the remaining dataset
    """
    pdb_ids = np.array(
        [fi.get_pdb_code(p[0]) for (p, _) in all_chain_sequences])
    split = set()
    idx = 0
    while len(split) < split_size:
        (rand_id, _) = all_chain_sequences[idx]
        pdb_code = fi.get_pdb_code(rand_id[0])
        hits = seq.find_cluster_members(pdb_code, clusterings)
        # ensure that at least min_fam_in_split families in each split
        if len(hits) > split_size / min_fam_in_split:
            idx += 1
            continue
        split = split.union(hits)
        idx += 1

    matches = np.array([i for i, x in enumerate(pdb_ids) if x in split])
    selected_chain_sequences = \
        [x for i, x in enumerate(all_chain_sequences) if i in matches]
    remaining_chain_sequences = \
        [x for i, x in enumerate(all_chain_sequences) if i not in matches]

    return selected_chain_sequences, remaining_chain_sequences


####################################
# split by calculating sequence identity
# to any example in training set
####################################


def identity_split(
        all_chain_sequences, cutoff, val_split=0.1, test_split=0.1,
        min_fam_in_split=5, blast_db=None, random_seed=None):
    """
    Splits pdb dataset using pre-computed sequence identity clusters from PDB.

    Generates train, val, test sets.

    Args:
        all_chain_sequences ((str, chain_sequences)[]):
            tuple of pdb ids and chain_sequences in dataset
        cutoff (float):
            sequence identity cutoff (can be .3, .4, .5, .7, .9, .95, 1.0)
        val_split (float): fraction of data used for validation. Default: 0.1
        test_split (float): fraction of data used for testing. Default: 0.1
        min_fam_in_split (int): controls variety of val/test sets. Default: 5
        blast_db (str):
            location of pre-computed BLAST DB for dataset. If None, compute and
            save in 'blast_db'. Default: None
        random_seed (int):  specifies random seed for shuffling. Default: None

    Returns:
        train_set (str[]):  pdbs in the train set
        val_set (str[]):  pdbs in the validation set
        test_set (str[]): pdbs in the test set

    """
    if blast_db is None:
        seq.write_to_blast_db(all_chain_sequences, 'blast_db')
        blast_db = 'blast_db'

    if random_seed is not None:
        np.random.seed(random_seed)

    pdb_codes = \
        np.unique([fi.get_pdb_code(x[0]) for (x, _) in all_chain_sequences])
    n = len(pdb_codes)
    test_size = n * test_split
    val_size = n * val_split

    np.random.shuffle(all_chain_sequences)

    logger.info('generating validation set...')
    val_set, all_chain_sequences = create_identity_split(
        all_chain_sequences, cutoff, val_size, min_fam_in_split, blast_db)
    logger.info('generating test set...')
    test_set, all_chain_sequences = create_identity_split(
        all_chain_sequences, cutoff, test_size, min_fam_in_split, blast_db)
    train_set = all_chain_sequences

    train_set = [x[0] for x in train_set]
    val_set = [x[0] for x in val_set]
    test_set = [x[0] for x in test_set]

    logger.info(f'train size {len(train_set):}')
    logger.info(f'val size {len(val_set):}')
    logger.info(f'test size {len(test_set):}')

    return train_set, val_set, test_set


def create_identity_split(all_chain_sequences, cutoff, split_size,
                          min_fam_in_split, blast_db):
    """
    Create a split while retaining diversity specified by min_fam_in_split.
    Returns split and removes any pdbs in this split from the remaining dataset
    """
    dataset_size = len(all_chain_sequences)
    tmp = {x: y for (x, y) in all_chain_sequences}
    assert len(tmp) == len(all_chain_sequences)
    all_chain_sequences = tmp

    # Get structure tuple.
    split, used = set(), set()
    to_use = set(all_chain_sequences.keys())
    while len(split) < split_size:
        # Get random structure tuple and random chain_sequence.
        rstuple = random.sample(to_use, 1)[0]
        rcs = all_chain_sequences[rstuple]

        found = seq.find_similar(rcs, blast_db, cutoff, dataset_size)

        # Get structure tuples.
        found = set([seq.fasta_name_to_tuple(x)[0] for x in found])

        # ensure that at least min_fam_in_split families in each split
        max_fam_size = int(math.ceil(split_size / min_fam_in_split))
        split = split.union(list(found)[:max_fam_size])
        to_use = to_use.difference(found)
        used = used.union(found)

    selected_chain_sequences = \
        [(s, cs) for s, cs in all_chain_sequences.items() if s in split]
    remaining_chain_sequences = \
        [(s, cs) for s, cs in all_chain_sequences.items() if s in to_use]

    return selected_chain_sequences, remaining_chain_sequences

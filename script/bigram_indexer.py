#!/usr/bin/python3
# pylint: disable=I0011,E1102,E1133
"""
    Create term-document matrix with tf-idf values
"""
import string
import gc
import os
import re
import sys
import tarfile
import psycopg2, psycopg2.extras
import numpy as np
from sklearn.feature_extraction.text import TfidfTransformer, CountVectorizer
from scipy.sparse import linalg as LA
from optparse import OptionParser

CONN = psycopg2.connect("dbname='sharesci' user='sharesci' host='localhost' password='sharesci'")

def insert(sql, data):
    cursor = CONN.cursor()
    try:
        psycopg2.extras.execute_values(cursor, sql, data, page_size=1000)
    except psycopg2.Error as error:
        print("Database error occured while executing '", sql, "'", 'Data: ')
        print(len(data), data[:10], data[-10:])
        print(error.diag.message_primary)
    CONN.commit()
    cursor.close()

def get_database_size():
    cur = CONN.cursor()
    size = sys.maxsize
    try:
        cur.execute("select pg_database_size('sharesci')")
        size = cur.fetchall()[0][0]
    except psycopg2.Error as error:
        print('Failed to get database size', file=sys.stderr)
        print(error.diag.message_primary)
    CONN.commit()
    cur.close()
    return size

def get_doc_ids(text_ids):
    doc_ids = []
    cursor = CONN.cursor()
    sql = "SELECT _id FROM document WHERE text_id = '{0}'"
    for text_id in text_ids:
        try:
            cursor.execute(sql.format(text_id))
            data = cursor.fetchone()
            if data:
            	doc_ids.append(data[0])
            else:
                print("Warning: Could not find the doc_id for document {}".format(text_id))
        except psycopg2.Error as error:
            print('Failed to get doc_id', file=sys.stderr)
            print(error.diag.message_primary)
    CONN.commit()
    cursor.close()
    return doc_ids

def populate_tables(raw_tf, text_ids, terms, options):
    """Populate the idf, document, tf tables.

     Args:
        raw_tf (scipy.sparse): term-document matrix of term counts.
        doc_ids (obj:`list` of :obj:`str`): List of document ids.
        terms (obj:`list` of :obj:`str`): List of terms.

    Returns:
        None
    """
    tfidftransformer = TfidfTransformer(sublinear_tf=True, use_idf=False, norm=None)
    lnc = tfidftransformer.fit_transform(raw_tf)
    print("Calculating document lengths")
    doc_lengths = LA.norm(lnc, axis=1).reshape(-1, 1)
    print("Finished.")

    if options.new_docs:
        doc_table = np.hstack((text_ids.reshape(-1, 1), doc_lengths))

        sql = """INSERT INTO document (text_id, length)
                VALUES %s
                ON CONFLICT (text_id) DO UPDATE 
                    SET length=EXCLUDED.length"""

        print("Inserting data into document table.")
        insert(sql, doc_table.tolist())
        print("Data inserted.")

    gram_ids = []
    tf_values = []
    #bigram_terms = [[term.partition(' ')[0], term.partition(' ')[2]] for term in terms]
    bigram_terms = [[term, ''] for term in terms]
    df_values = np.zeros(len(bigram_terms), dtype=np.int8).tolist()
    bigram_length = len(bigram_terms)

    rows, cols = lnc.nonzero()
    for col in cols:
        df_values[col] += 1 #calculate document frequency

    print("Inserting {} bigrams".format(bigram_length))

    m = n = 0
    while m < bigram_length:
        n = (m+10000) if (m+10000) <= bigram_length else m+(bigram_length%10000);
        cursor = CONN.cursor()
        try:
            cursor.callproc('insert_bigram_df', [bigram_terms[m:n], df_values[m:n]])
            data = cursor.fetchone()
            if data:
                gram_ids += data[0]
            else:
                print("Warning: insert_bigram_df function returned null.")
        except psycopg2.Error as error:
            print(error)
        CONN.commit()
        cursor.close()
        m += 10000
        if bigram_length-m > 0 and (m/1000000).is_integer():
            print("{} bigrams remaining.".format(bigram_length-m))
    print("Data Inserted")
    df_values = None
    bigram_terms = None

    print("Getting doc_ids from text_ids.")
    doc_ids = get_doc_ids(text_ids)
    print("Calculating tf values.")
    for row, col in zip(rows, cols):
        tf_values.append([gram_ids[col], doc_ids[row], float(lnc[row, col]/doc_lengths[row])])

    print("Inserting {} rows into tf table".format(len(tf_values)))
    sql = """INSERT INTO tf(gram_id, doc_id, lnc)
             VALUES %s
             ON CONFLICT (gram_id, doc_id) DO UPDATE 
                SET lnc=EXCLUDED.lnc"""
    insert(sql, tf_values)
    print("Data Inserted.")

def load_files(root, mappings):
    """Load all the regular files from all the archive files

     Args:
        root (obj: `str`): Full path of the folder which contains all .tar.gz files

    Returns:
        None
    """
    token_dict = {}

    for subdir, _, tar_files in os.walk(root):
        print("Processing Files\n")
        for tar_file in tar_files:
            if tar_file.endswith(".tar.gz"):
                tar_file_path = subdir + os.path.sep + tar_file
                tar = tarfile.open(tar_file_path)
                for member in tar.getmembers():
                    file = tar.extractfile(member)
                    if file is not None: #only read regular files
                        doc_id = re.sub(r'.preproc$', '', os.path.basename(member.name))
                        if doc_id in mappings[0]:
                            doc_id = mappings[1][mappings[0].index(doc_id)]
                        print("Processing {0}".format(member.name), end="\r")
                        text = file.read().decode("utf-8")
                        text = text.translate(str.maketrans('', '', string.punctuation))
                        token_dict[doc_id] = text
        print("Processing Complete.")

    return token_dict

if __name__ == "__main__":

    PARSER = OptionParser()
    PARSER.add_option("-d", dest="doc_dir")
    PARSER.add_option("-m", dest="mapping_file")
    PARSER.add_option("--new-docs", action="store_true", default=False, dest="new_docs")

    (OPTIONS, ARGS) = PARSER.parse_args()

    MAX_DATABASE_SIZE = 100*1000*1000*1000  # 100 GB
    if get_database_size() > MAX_DATABASE_SIZE:
        print("Database is too big! Can't fit more data within the limit!", file=sys.stderr)
        sys.exit(1)
    if OPTIONS.doc_dir:
        mappings = [[], []]
        if OPTIONS.mapping_file:
            with open(OPTIONS.mapping_file) as f:
                data = eval(f.readline())
                for d in data:
                    mappings[0].append(d["arXiv_id"])
                    mappings[1].append(d["_id"])

        TOKEN_DICT = load_files(OPTIONS.doc_dir, mappings)
        print("Calculating raw tf values.")
        #VECTORIZER = CountVectorizer(token_pattern=r'(?u)\b\w[A-Za-z_-]{1,19}\b', ngram_range=(2, 2))
        VECTORIZER = CountVectorizer(token_pattern=r'(?u)\b\w[A-Za-z_-]{1,19}\b', ngram_range=(1, 1))
        RAW_TF = VECTORIZER.fit_transform(TOKEN_DICT.values())
        print("Calculation of raw tf values complete.")
        TERMS = VECTORIZER.get_feature_names()
        DOC_IDS = np.array(list(TOKEN_DICT.keys()))

        # Attempt to reduce memory usage by destroying potentially large objects
        TOKEN_DICT = None
        VECTORIZER = None
        mappings = None
        gc.collect()
            
        populate_tables(RAW_TF, DOC_IDS, TERMS, OPTIONS)

        print("All done.")
    else:
        print("Please specify path to the folder which contains all .tar.gz")

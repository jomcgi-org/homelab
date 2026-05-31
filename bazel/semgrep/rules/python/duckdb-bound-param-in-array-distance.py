# Tests for duckdb-bound-param-in-array-distance rule.
# DuckDB HNSW index scan only engages when the query vector is a plan-time
# constant.  Passing the vector as a bound parameter ($name or $1) forces a
# sequential scan and defeats the index entirely.
import duckdb


def bad_fstring_double_quote():
    # ruleid: duckdb-bound-param-in-array-distance
    query = f"SELECT id FROM chunks ORDER BY array_distance(embedding, $query_vec::FLOAT[512]) LIMIT 10"
    return conn.execute(query, {"query_vec": [0.1] * 512}).fetchall()


def bad_regular_string_positional():
    # ruleid: duckdb-bound-param-in-array-distance
    query = "SELECT id FROM chunks ORDER BY array_distance(embedding, $1::FLOAT[512]) LIMIT 10"
    return conn.execute(query, [[0.1] * 512]).fetchall()


def bad_fstring_single_quote():
    vec_param = "$query_embedding"
    # ruleid: duckdb-bound-param-in-array-distance
    query = f'SELECT id FROM docs ORDER BY array_distance(emb, $query_embedding::FLOAT[768]) LIMIT 5'
    return conn.execute(query, {"query_embedding": [0.0] * 768}).fetchall()


def bad_named_param():
    # ruleid: duckdb-bound-param-in-array-distance
    sql = "SELECT id, score FROM vectors ORDER BY array_distance(vec, $search_vec::FLOAT[256])"
    return conn.execute(sql, {"search_vec": vector}).fetchall()


def ok_inline_python_interpolation():
    vec = [0.1] * 512
    # ok: vector is interpolated as a Python value — not a DuckDB bound param
    query = f"SELECT id FROM chunks ORDER BY array_distance(embedding, {vec}::FLOAT[512]) LIMIT 10"
    return conn.execute(query).fetchall()


def ok_no_array_distance():
    # ok: query uses a bound param but not in array_distance
    query = f"SELECT id FROM chunks WHERE category = $category"
    return conn.execute(query, {"category": "news"}).fetchall()


def ok_no_bound_param_in_distance():
    # ok: array_distance uses a literal vector, no bound param
    query = "SELECT id FROM chunks ORDER BY array_distance(embedding, [0.1, 0.2, 0.3]::FLOAT[3]) LIMIT 5"
    return conn.execute(query).fetchall()

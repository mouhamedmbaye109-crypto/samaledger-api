import os
from contextlib import contextmanager
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

pool = ConnectionPool(os.environ["DATABASE_URL"], kwargs={"row_factory": dict_row}, open=False)


@contextmanager
def tx(user_id=None):
    """Une transaction ; l'utilisateur est transmis à la base pour le journal d'audit."""
    with pool.connection() as conn:
        with conn.transaction():
            if user_id:
                conn.execute("SELECT set_config('app.user_id', %s, true)", (str(user_id),))
            yield conn

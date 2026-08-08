# -*- coding: utf-8 -*-


def migrate(cr, version):
    """Tag legacy Live Band history as E1 and recover its expiration.

    New hourly cycles create independent E1/E2/E3 rows. Existing rows were
    nearest-expiry observations, so keeping them as E1 preserves the useful
    historical envelope without pretending E2/E3 history existed.
    """
    cr.execute("""
        UPDATE dankbit_live_band
        SET expiry_index = 0
        WHERE expiry_index IS NULL
    """)
    cr.execute("""
        UPDATE dankbit_live_band AS lb
        SET expiration = src.expiration
        FROM (
            SELECT SUBSTRING(name FROM '^[^-]+-[^-]+') AS instrument,
                   MIN(expiration) AS expiration
            FROM dankbit_trade
            GROUP BY SUBSTRING(name FROM '^[^-]+-[^-]+')
        ) AS src
        WHERE lb.expiration IS NULL
          AND lb.instrument = src.instrument
    """)

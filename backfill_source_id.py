#!/usr/bin/env python3
import pymysql


def main():
    conn = pymysql.connect(
        host="qq.rwlb.rds.aliyuncs.com",
        user="data",
        password="AbHGL8jMwMPmzM",
        database="data",
        port=3306,
        connect_timeout=5,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        total_updated = 0
        with conn.cursor() as cursor:
            while True:
                cursor.execute(
                    """
                    UPDATE xhs_initial_state_capture r
                    JOIN (
                      SELECT id, SUBSTRING_INDEX(SUBSTRING_INDEX(url, '/explore/', -1), '?', 1) AS note_id_extracted
                      FROM xhs_initial_state_capture
                      WHERE source_id IS NULL
                      LIMIT 500
                    ) t ON t.id = r.id
                    JOIN xhs_url s
                      ON s.note_id = (t.note_id_extracted COLLATE utf8mb4_general_ci)
                    SET r.source_id = s.id
                    WHERE r.source_id IS NULL
                    """
                )
                batch_updated = cursor.rowcount
                total_updated += batch_updated
                print("batch_updated", batch_updated, "total_updated", total_updated, flush=True)
                if batch_updated == 0:
                    break
            print("updated", total_updated)
            cursor.execute("SELECT COUNT(*) FROM xhs_initial_state_capture WHERE source_id IS NOT NULL")
            print("filled", cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM xhs_initial_state_capture")
            print("total", cursor.fetchone()[0])
    finally:
        conn.close()


if __name__ == "__main__":
    main()

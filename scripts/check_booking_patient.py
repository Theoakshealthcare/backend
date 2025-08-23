import psycopg2

DB_URL = "postgresql://postgres.ljrcyoonkqpbdsddqmwk:Oaklycarehealth@aws-0-eu-north-1.pooler.supabase.com:6543/postgres"
BOOKING_ID = "3888e9f9-a024-4ebf-8b3b-55a4a0b18901"
EXPECTED_PATIENT_ID = "a1982f60-fffb-4372-8dbf-b62b418b903b"

conn = psycopg2.connect(DB_URL)
cur = conn.cursor()
cur.execute("SELECT id, patient_id, caregiver_id, service_type FROM bookings WHERE id = %s", (BOOKING_ID,))
row = cur.fetchone()
if row:
    print(f"Booking found: {row}")
    if str(row[1]) == EXPECTED_PATIENT_ID:
        print("Patient ID matches expected value.")
    else:
        print(f"Patient ID does NOT match. Found: {row[1]}")
else:
    print("Booking not found.")
cur.close()
conn.close() 
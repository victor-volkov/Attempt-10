import sqlite3

conn = sqlite3.connect('twitter_accounts.db')
c = conn.cursor()

# Show all tables and their structure
c.execute("SELECT * FROM sqlite_master WHERE type='table';")
tables = c.fetchall()
print("\nTables in database:")
for table in tables:
    print(f"\nTable: {table[1]}")
    c.execute(f"PRAGMA table_info({table[1]})")
    columns = c.fetchall()
    print("Columns:")
    for col in columns:
        print(f"- {col[1]} ({col[2]})")

# Check twitter_accounts data
print("\nTwitter Accounts Data:")
c.execute('''
    SELECT 
        id,
        account_name,
        persona_id,
        automation_persona_id,
        is_active,
        automation_mode
    FROM twitter_accounts
''')
accounts = c.fetchall()
for acc in accounts:
    print(f"\nAccount ID: {acc[0]}")
    print(f"Name: {acc[1]}")
    print(f"Persona ID: {acc[2]}")
    print(f"Automation Persona ID: {acc[3]}")
    print(f"Is Active: {acc[4]}")
    print(f"Automation Mode: {acc[5]}")

# Check personas data
print("\nPersonas Data:")
c.execute('''
    SELECT 
        id,
        name,
        description,
        style,
        length,
        use_emoji,
        use_hashtags,
        mention_user
    FROM personas
''')
personas = c.fetchall()
for p in personas:
    print(f"\nPersona ID: {p[0]}")
    print(f"Name: {p[1]}")
    print(f"Description: {p[2]}")
    print(f"Style: {p[3]}")
    print(f"Length: {p[4]}")
    print(f"Use Emoji: {p[5]}")
    print(f"Use Hashtags: {p[6]}")
    print(f"Mention User: {p[7]}")

# Check relationships
print("\nAccount-Persona Relationships:")
c.execute('''
    SELECT 
        ta.id as account_id,
        ta.account_name,
        ta.persona_id,
        p1.name as persona_name,
        ta.automation_persona_id,
        p2.name as automation_persona_name
    FROM twitter_accounts ta
    LEFT JOIN personas p1 ON ta.persona_id = p1.id
    LEFT JOIN personas p2 ON ta.automation_persona_id = p2.id
''')
relationships = c.fetchall()
for rel in relationships:
    print(f"\nAccount: {rel[1]} (ID: {rel[0]})")
    print(f"Regular Persona: {rel[3]} (ID: {rel[2]})")
    print(f"Automation Persona: {rel[5]} (ID: {rel[4]})")

conn.close() 
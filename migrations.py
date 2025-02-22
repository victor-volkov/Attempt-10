import sqlite3
from sqlite3 import Error

def migrate_mention_user():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Check if mention_user column exists in twitter_accounts
        c.execute("PRAGMA table_info(twitter_accounts)")
        columns = [col[1] for col in c.fetchall()]
        
        if 'mention_user' in columns:
            # Get all accounts with their mention_user setting and persona_id
            c.execute('''
                SELECT id, mention_user, automation_persona_id 
                FROM twitter_accounts 
                WHERE automation_persona_id IS NOT NULL
            ''')
            accounts = c.fetchall()
            
            # Update each associated persona
            for account_id, mention_user, persona_id in accounts:
                c.execute('''
                    UPDATE personas 
                    SET mention_user = ? 
                    WHERE id = ?
                ''', (mention_user, persona_id))
            
            # Remove mention_user column from twitter_accounts
            c.execute('ALTER TABLE twitter_accounts DROP COLUMN mention_user')
            
            conn.commit()
            print("Successfully migrated mention_user settings to personas table")
        else:
            print("mention_user column not found in twitter_accounts table - no migration needed")
            
    except Error as e:
        print(f"Database error: {e}")
    finally:
        if conn:
            conn.close()

def migrate_rss_feeds():
    try:
        conn = sqlite3.connect('twitter_accounts.db')
        c = conn.cursor()
        
        # Create RSS feeds table
        c.execute('''
            CREATE TABLE IF NOT EXISTS rss_feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL UNIQUE,
                description TEXT,
                last_fetch TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Create RSS posting configurations table
        c.execute('''
            CREATE TABLE IF NOT EXISTS rss_posting_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                feed_id INTEGER NOT NULL,
                persona_id INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT 0,
                posts_per_day INTEGER DEFAULT 1,
                schedule_settings TEXT, -- JSON with days/hours configuration
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES twitter_accounts(id),
                FOREIGN KEY (feed_id) REFERENCES rss_feeds(id),
                FOREIGN KEY (persona_id) REFERENCES personas(id)
            )
        ''')
        
        conn.commit()
    except Exception as e:
        print(f"Error in migrate_rss_feeds: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    migrate_mention_user()
    migrate_rss_feeds() 
"""Initialize local database schema for the Etsy tool MVP."""

from app.db import create_database_schema


if __name__ == "__main__":
    create_database_schema()
    print("Database schema created.")

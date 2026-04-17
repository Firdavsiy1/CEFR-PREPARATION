#!/bin/bash
echo "Starting PostgreSQL configuration..."

# Start PostgreSQL service
sudo systemctl start postgresql

# Fix collation mismatches (common after OS updates)
sudo -u postgres psql -c "ALTER DATABASE template1 REFRESH COLLATION VERSION;"
sudo -u postgres psql -c "ALTER DATABASE postgres REFRESH COLLATION VERSION;"

# Create Database and User
sudo -u postgres psql -c "CREATE USER cefr_user WITH PASSWORD 'cefr_pass';" 2>/dev/null || true
sudo -u postgres psql -c "CREATE DATABASE cefr_db OWNER cefr_user;"
sudo -u postgres psql -c "ALTER ROLE cefr_user SET client_encoding TO 'utf8';"
sudo -u postgres psql -c "ALTER ROLE cefr_user SET default_transaction_isolation TO 'read committed';"
sudo -u postgres psql -c "ALTER ROLE cefr_user SET timezone TO 'UTC';"

echo "Permissions and user setup complete!"

# Run migrations
echo "Applying migrations to PostgreSQL..."
venv/bin/python manage.py migrate

# Load SQLite data into PostgreSQL
echo "Restoring data from SQLite backup..."
venv/bin/python manage.py loaddata datadump.json

echo "Done! The application is now using PostgreSQL."

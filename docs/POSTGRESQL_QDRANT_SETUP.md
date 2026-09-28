# PostgreSQL and Qdrant Setup Guide

## Overview

This guide explains how to set up and use PostgreSQL and Qdrant with Meeting-Ops for enhanced performance and semantic search capabilities.

## Why PostgreSQL and Qdrant?

### PostgreSQL Benefits
- **Better Performance**: Optimized for concurrent operations and large datasets
- **Full-text Search**: Built-in text search capabilities with GIN indexes
- **JSONB Support**: Efficient storage and querying of JSON data
- **Reliability**: ACID compliance and proven stability
- **Scalability**: Handles large amounts of data efficiently

### Qdrant Benefits
- **Semantic Search**: Find similar content based on meaning, not just keywords
- **Fast Vector Search**: Optimized for high-dimensional vector operations
- **Filtering**: Combine semantic search with metadata filters
- **Real-time Indexing**: Index new transcriptions instantly
- **Scalability**: Handles millions of vectors efficiently

## Quick Start

### 1. Start the PostgreSQL Stack

```bash
# Start Meeting-Ops with PostgreSQL and Qdrant
./start-postgres-stack.sh
```

This script will:
- Start PostgreSQL on port 5432
- Start Qdrant on port 6333
- Automatically migrate data from SQLite (if it exists)
- Set up all necessary database schemas

### 2. Verify Services

```bash
# Check service status
docker-compose ps

# View logs
docker-compose logs -f
```

### 3. Access Services

- **Frontend**: http://localhost:7777
- **Backend API**: http://localhost:9050
- **PostgreSQL**: localhost:5432
  - Database: `meeting_ops`
  - Username: `meeting_ops`
  - Password: `unicorn2025` (or value of `DB_PASSWORD`)
- **Qdrant**: http://localhost:6333
- **pgAdmin** (optional): http://localhost:5050
  - Email: `admin@unicorn.local`
  - Password: `admin123` (or value of `PGADMIN_PASSWORD`)

## Manual Migration from SQLite

If you have existing data in SQLite and want to migrate manually:

```bash
# Set environment variables
export DATABASE_URL="postgresql://meeting_ops:unicorn2025@localhost:5432/meeting_ops"
export SQLITE_PATH="./backend/meeting_sessions.db"

# Run migration
cd backend
python migrate_to_postgres.py
```

## Using Semantic Search

### Search API Endpoints

#### 1. Semantic Search
```bash
# Search for content semantically
curl -X POST http://localhost:9050/api/search/semantic \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "query": "discussion about project timeline",
    "limit": 10,
    "score_threshold": 0.5
  }'
```

#### 2. Find Similar Segments
```bash
# Find segments similar to provided text
curl -X POST http://localhost:9050/api/search/similar \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "text": "We need to focus on customer feedback",
    "limit": 5
  }'
```

#### 3. Index a Session
```bash
# Index or re-index all transcriptions for a session
curl -X POST http://localhost:9050/api/search/index/session/SESSION_ID \
  -H "Authorization: Bearer YOUR_TOKEN"
```

#### 4. Collection Info
```bash
# Get information about the vector search collection
curl http://localhost:9050/api/search/collection/info \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Configuration

### Environment Variables

```bash
# PostgreSQL Configuration
DATABASE_URL=postgresql://meeting_ops:unicorn2025@localhost:5432/meeting_ops
DB_PASSWORD=unicorn2025

# Qdrant Configuration
QDRANT_URL=http://localhost:6333
QDRANT_COLLECTION=meeting_transcripts

# Optional: Run migration on startup
RUN_MIGRATION=true
```

### Docker Compose Override

The `docker-compose.override.yml` file automatically configures PostgreSQL and Qdrant when present. To disable it:

```bash
# Rename or remove the override file
mv docker-compose.override.yml docker-compose.override.yml.disabled
```

## Database Management

### Using pgAdmin

1. Start pgAdmin:
   ```bash
   docker-compose --profile tools up -d pgadmin
   ```

2. Access at http://localhost:5050

3. Add server connection:
   - Host: `postgres` (or `localhost` if not in Docker)
   - Port: `5432`
   - Database: `meeting_ops`
   - Username: `meeting_ops`
   - Password: `unicorn2025`

### Direct PostgreSQL Access

```bash
# Connect to PostgreSQL
docker exec -it unicorn-postgres psql -U meeting_ops -d meeting_ops

# Or from host
PGPASSWORD=unicorn2025 psql -h localhost -U meeting_ops -d meeting_ops
```

### Useful Queries

```sql
-- Check transcription count
SELECT COUNT(*) FROM transcriptions;

-- Find recent sessions
SELECT session_id, name, created_at 
FROM sessions 
ORDER BY created_at DESC 
LIMIT 10;

-- Search transcriptions (basic)
SELECT * FROM transcriptions 
WHERE text ILIKE '%customer%' 
LIMIT 10;

-- Full-text search (PostgreSQL)
SELECT * FROM transcriptions 
WHERE to_tsvector('english', text) @@ plainto_tsquery('english', 'customer feedback');
```

## Qdrant Management

### Access Qdrant Dashboard

Open http://localhost:6333/dashboard in your browser.

### Using Qdrant API

```bash
# Get collections
curl http://localhost:6333/collections

# Get collection info
curl http://localhost:6333/collections/meeting_transcripts

# Search directly in Qdrant
curl -X POST http://localhost:6333/collections/meeting_transcripts/points/search \
  -H "Content-Type: application/json" \
  -d '{
    "vector": [0.1, 0.2, 0.3, ...],
    "limit": 10
  }'
```

## Backup and Restore

### PostgreSQL Backup

```bash
# Backup database
docker exec unicorn-postgres pg_dump -U meeting_ops meeting_ops > backup.sql

# Restore database
docker exec -i unicorn-postgres psql -U meeting_ops meeting_ops < backup.sql
```

### Qdrant Backup

```bash
# Create snapshot
curl -X POST http://localhost:6333/collections/meeting_transcripts/snapshots

# Download snapshot
curl http://localhost:6333/collections/meeting_transcripts/snapshots/SNAPSHOT_NAME > qdrant_backup.snapshot
```

## Troubleshooting

### Services Not Starting

```bash
# Check logs
docker-compose logs postgres
docker-compose logs qdrant
docker-compose logs backend

# Restart services
docker-compose restart
```

### Migration Issues

```bash
# Check migration logs
docker-compose logs backend | grep -i migration

# Run migration manually
docker exec -it unicorn-backend python migrate_to_postgres.py
```

### Performance Tuning

1. **PostgreSQL**: Edit `docker-compose.override.yml` to add:
   ```yaml
   postgres:
     command: 
       - "postgres"
       - "-c"
       - "shared_buffers=256MB"
       - "-c"
       - "work_mem=16MB"
   ```

2. **Qdrant**: Adjust in `docker-compose.override.yml`:
   ```yaml
   qdrant:
     environment:
       QDRANT__SERVICE__MAX_REQUEST_SIZE_MB: 50
       QDRANT__STORAGE__PERFORMANCE__INDEXING_THRESHOLD_KB: 20000
   ```

## Advanced Features

### Hybrid Search

Combine semantic search with metadata filters:

```json
{
  "query": "project deadline",
  "session_id": "session_12345",
  "speaker": "John Doe",
  "start_date": "2024-01-01T00:00:00Z",
  "end_date": "2024-12-31T23:59:59Z",
  "score_threshold": 0.6
}
```

### Batch Indexing

Re-index all sessions (admin only):

```bash
curl -X POST http://localhost:9050/api/search/reindex-all \
  -H "Authorization: Bearer ADMIN_TOKEN"
```

### Custom Embeddings

The system uses `all-MiniLM-L6-v2` by default. To use a different model, modify `services/vector_search.py`:

```python
embedding_model = "sentence-transformers/all-mpnet-base-v2"  # Higher quality
# or
embedding_model = "sentence-transformers/paraphrase-MiniLM-L3-v2"  # Faster
```

## Security Considerations

1. **Change Default Passwords**: Update `DB_PASSWORD` and `PGADMIN_PASSWORD`
2. **Network Security**: Use firewall rules to restrict database access
3. **SSL/TLS**: Enable SSL for PostgreSQL in production
4. **Backup Encryption**: Encrypt database backups
5. **Access Control**: Use PostgreSQL roles for fine-grained permissions

## Next Steps

1. **Explore the API**: Use the semantic search endpoints in your applications
2. **Monitor Performance**: Check query performance and optimize as needed
3. **Scale Up**: Consider clustering for high-availability setups
4. **Integration**: Connect Meeting-Ops search to your existing workflows
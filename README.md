# Content Finder

Searches in the internet archive, an archive of public domain torrents (https://www.publicdomaintorrents.info/), and Project Gutenberg for freely available content. It can also download them as either files, or torrents.

Integrated with rqbit to download larger files/torrents. It was selected because it has a clean web api. It is expected that you provide it via docker compose.

Environment variables:
- DOWNLOAD_DIR=/data/downloads, any file that could not be recognized as a book or video
- BOOKS_DOWNLOAD_DIR=/data/books, recognized books and comics
- VIDEOS_DOWNLOAD_DIR=/data/videos, recognized videos and movies
- RQBIT_URL=http://rqbit:3030, the url of the rqbit instance, the default is a docker compose service named rqbit

Database connection information is in the DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,DB_SSLMODE variables.
SSLMODE can be set to disable, allow, prefer, require, verify-ca, or verify-full. The default is prefer.
All of them can take values from environment variables, or files.

IF no database is provided, it will use a sqlite database in /data/contentfind.db


Currently in an experimental state, I know that the code was working but was not deployed in an actual environment! There may be configuration issues.

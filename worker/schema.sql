-- Cards sent to Telegram, so the Worker can resolve a button's short id to a listing.
CREATE TABLE IF NOT EXISTS listings (
    sid TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    lat REAL,
    lng REAL,
    url TEXT,
    address TEXT,
    cost INTEGER
);
-- info: JSON {lat, lng, url, address, cost, missing}
CREATE TABLE IF NOT EXISTS saved (key TEXT PRIMARY KEY, info TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hidden (key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS hidden_dev (lat REAL NOT NULL, lng REAL NOT NULL, PRIMARY KEY (lat, lng));
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT);

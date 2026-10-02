-- Runs once, on first initialisation of the Postgres volume.
-- Creates the dedicated test database alongside the main one.
CREATE DATABASE agentcore_test OWNER agentcore;
\connect agentcore_test
CREATE EXTENSION IF NOT EXISTS vector;

-- Content-addressed vectors are shared by task admission and session workers.
CREATE TABLE knowledge.recall_embeddings (
    key text PRIMARY KEY,
    embedding vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- A voice or interactive session may be opened before its first user request.
ALTER TABLE agent_sessions.agent_sessions
    ADD COLUMN recall_pending boolean NOT NULL DEFAULT false;

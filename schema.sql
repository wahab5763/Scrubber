-- One batch represents a single upload operation and owns its documents and analysis.
CREATE TABLE IF NOT EXISTS batches (
    id uuid PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Store each scrubbed file separately and preserve its order within the upload.
CREATE TABLE IF NOT EXISTS documents (
    batch_id uuid NOT NULL REFERENCES batches ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    filename text NOT NULL,
    scrubbed_text text NOT NULL CHECK (length(scrubbed_text) > 0),
    PRIMARY KEY (batch_id, ordinal)
);

-- Keep the latest analysis as flexible JSON without duplicating document text.
CREATE TABLE IF NOT EXISTS analyses (
    batch_id uuid PRIMARY KEY REFERENCES batches ON DELETE CASCADE,
    result jsonb NOT NULL
);

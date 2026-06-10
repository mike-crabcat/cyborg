-- Add persona flag and model override for local subagents
ALTER TABLE subagents ADD COLUMN persona INTEGER NOT NULL DEFAULT 0;
ALTER TABLE subagents ADD COLUMN model TEXT NOT NULL DEFAULT '';

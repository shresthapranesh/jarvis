-- What `Base.metadata.create_all` (db/models.py) makes, captured from a fresh
-- database in creation order: each table, then its indexes by name. `schema.rs`
-- creates every table missing here (and its indexes), then migrates.
-- Generated: JARVIS_UPDATE_GOLDEN=1 uv run pytest tests/test_edge_schema.py

CREATE TABLE projects (
	id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	description TEXT, 
	instructions TEXT NOT NULL, 
	memory TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE automations (
	id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	description TEXT, 
	input_type VARCHAR NOT NULL, 
	prompt_text TEXT, 
	model VARCHAR, 
	code_text TEXT, 
	webhook_url VARCHAR, 
	webhook_method VARCHAR, 
	webhook_headers TEXT, 
	webhook_body TEXT, 
	schedule VARCHAR, 
	enabled BOOLEAN NOT NULL, 
	stateful BOOLEAN NOT NULL, 
	notifications TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE config_settings (
	"key" VARCHAR NOT NULL, 
	value TEXT NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY ("key")
);

CREATE TABLE notification_channels (
	id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	type VARCHAR NOT NULL, 
	target VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE workflows (
	id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	description TEXT, 
	definition TEXT NOT NULL, 
	notifications TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE board_tasks (
	id VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	body TEXT, 
	status VARCHAR NOT NULL, 
	priority INTEGER NOT NULL, 
	created_by VARCHAR NOT NULL, 
	model VARCHAR, 
	skill VARCHAR, 
	blocked_reason TEXT, 
	blocked_kind VARCHAR, 
	pending_answer TEXT, 
	failure_count INTEGER NOT NULL, 
	summary TEXT, 
	result_metadata TEXT, 
	job_id VARCHAR, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_board_tasks_status ON board_tasks (status);

CREATE TABLE approvals (
	id VARCHAR NOT NULL, 
	source VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	question TEXT NOT NULL, 
	label VARCHAR NOT NULL, 
	tool VARCHAR, 
	args_json TEXT, 
	task_id VARCHAR, 
	interrupt_id VARCHAR, 
	parent_id VARCHAR, 
	board_task_id VARCHAR, 
	action VARCHAR, 
	action_payload TEXT, 
	result TEXT, 
	answer TEXT, 
	requested_at DATETIME NOT NULL, 
	resolved_at DATETIME, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_approvals_board_task_id ON approvals (board_task_id);

CREATE INDEX ix_approvals_source ON approvals (source);

CREATE INDEX ix_approvals_status ON approvals (status);

CREATE INDEX ix_approvals_task_id ON approvals (task_id);

CREATE TABLE jobs (
	id VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	payload TEXT NOT NULL, 
	status VARCHAR NOT NULL, 
	run_at DATETIME NOT NULL, 
	attempts INTEGER NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	last_error TEXT, 
	locked_by VARCHAR, 
	locked_until DATETIME, 
	cancel_requested BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	completed_at DATETIME, 
	thread_id VARCHAR, 
	runtime VARCHAR, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_jobs_kind_status_run_at ON jobs (kind, status, run_at);

CREATE INDEX ix_jobs_locked_until ON jobs (locked_until);

CREATE UNIQUE INDEX ux_jobs_thread_lease ON jobs (thread_id) WHERE status = 'running' AND thread_id IS NOT NULL;

CREATE TABLE memories (
	id VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	text TEXT NOT NULL, 
	embedding BLOB, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_memories_kind ON memories (kind);

CREATE TABLE skills (
	id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	description TEXT NOT NULL, 
	body TEXT NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	embedding BLOB, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_skills_name ON skills (name);

CREATE TABLE thread_messages (
	id VARCHAR NOT NULL, 
	thread_id VARCHAR NOT NULL, 
	seq INTEGER NOT NULL, 
	message_id VARCHAR, 
	role VARCHAR NOT NULL, 
	data TEXT NOT NULL, 
	evicted_at DATETIME, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_thread_messages_thread_message ON thread_messages (thread_id, message_id);

CREATE UNIQUE INDEX ux_thread_messages_thread_seq ON thread_messages (thread_id, seq);

CREATE TABLE thread_state (
	thread_id VARCHAR NOT NULL, 
	todos TEXT, 
	source VARCHAR, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (thread_id)
);

CREATE TABLE transcript_blobs (
	hash VARCHAR NOT NULL, 
	mime_type VARCHAR NOT NULL, 
	size INTEGER NOT NULL, 
	data BLOB NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (hash)
);

CREATE TABLE kv_store (
	namespace VARCHAR NOT NULL, 
	"key" VARCHAR NOT NULL, 
	value TEXT NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (namespace, "key")
);

CREATE TABLE conversations (
	id VARCHAR NOT NULL, 
	title VARCHAR, 
	model VARCHAR NOT NULL, 
	surface VARCHAR NOT NULL, 
	pinned BOOLEAN NOT NULL, 
	ephemeral BOOLEAN NOT NULL, 
	project_id VARCHAR, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE INDEX ix_conversations_ephemeral ON conversations (ephemeral);

CREATE INDEX ix_conversations_project_id ON conversations (project_id);

CREATE INDEX ix_conversations_surface ON conversations (surface);

CREATE TABLE automation_runs (
	id VARCHAR NOT NULL, 
	automation_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	triggered_by VARCHAR NOT NULL, 
	output TEXT, 
	error TEXT, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(automation_id) REFERENCES automations (id)
);

CREATE INDEX ix_automation_runs_automation_id ON automation_runs (automation_id);

CREATE TABLE workflow_runs (
	id VARCHAR NOT NULL, 
	workflow_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	inputs TEXT, 
	outputs TEXT, 
	node_results TEXT, 
	error TEXT, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(workflow_id) REFERENCES workflows (id)
);

CREATE INDEX ix_workflow_runs_workflow_id ON workflow_runs (workflow_id);

CREATE TABLE board_task_links (
	id VARCHAR NOT NULL, 
	parent_id VARCHAR NOT NULL, 
	child_id VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(parent_id) REFERENCES board_tasks (id), 
	FOREIGN KEY(child_id) REFERENCES board_tasks (id)
);

CREATE INDEX ix_board_task_links_child_id ON board_task_links (child_id);

CREATE UNIQUE INDEX ix_board_task_links_edge ON board_task_links (parent_id, child_id);

CREATE INDEX ix_board_task_links_parent_id ON board_task_links (parent_id);

CREATE TABLE conversation_episodes (
	id VARCHAR NOT NULL, 
	conversation_id VARCHAR NOT NULL, 
	text TEXT NOT NULL, 
	embedding BLOB, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id)
);

CREATE INDEX ix_conversation_episodes_conversation_id ON conversation_episodes (conversation_id);

CREATE TABLE messages (
	id VARCHAR NOT NULL, 
	conversation_id VARCHAR NOT NULL, 
	role VARCHAR NOT NULL, 
	content TEXT NOT NULL, 
	model VARCHAR, 
	status VARCHAR NOT NULL, 
	input_tokens INTEGER, 
	output_tokens INTEGER, 
	ttft_ms FLOAT, 
	llm_ms FLOAT, 
	prefill_tps FLOAT, 
	eval_tps FLOAT, 
	duration_ms FLOAT, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id)
);

CREATE INDEX ix_messages_conv_created ON messages (conversation_id, created_at);

CREATE INDEX ix_messages_conversation_id ON messages (conversation_id);

CREATE TABLE memory_activities (
	id VARCHAR NOT NULL, 
	memory_id VARCHAR NOT NULL, 
	conversation_id VARCHAR, 
	kind VARCHAR NOT NULL, 
	score FLOAT, 
	"query" TEXT, 
	source VARCHAR NOT NULL, 
	accessed_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(memory_id) REFERENCES memories (id) ON DELETE CASCADE, 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id) ON DELETE SET NULL
);

CREATE INDEX ix_mem_act_conv_time ON memory_activities (conversation_id, accessed_at);

CREATE INDEX ix_mem_act_mem_time ON memory_activities (memory_id, accessed_at);

CREATE INDEX ix_memory_activities_accessed_at ON memory_activities (accessed_at);

CREATE INDEX ix_memory_activities_conversation_id ON memory_activities (conversation_id);

CREATE INDEX ix_memory_activities_kind ON memory_activities (kind);

CREATE INDEX ix_memory_activities_memory_id ON memory_activities (memory_id);

CREATE INDEX ix_memory_activities_source ON memory_activities (source);

CREATE TABLE steps (
	id VARCHAR NOT NULL, 
	message_id VARCHAR NOT NULL, 
	conversation_id VARCHAR NOT NULL, 
	node VARCHAR NOT NULL, 
	source VARCHAR NOT NULL, 
	subagent VARCHAR, 
	data TEXT, 
	seq INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(message_id) REFERENCES messages (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id)
);

CREATE INDEX ix_steps_conversation_id ON steps (conversation_id);

CREATE INDEX ix_steps_message_id ON steps (message_id);

CREATE TABLE artifacts (
	id VARCHAR NOT NULL, 
	title VARCHAR NOT NULL, 
	filename VARCHAR NOT NULL, 
	kind VARCHAR NOT NULL, 
	mime_type VARCHAR, 
	conversation_id VARCHAR, 
	message_id VARCHAR, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(conversation_id) REFERENCES conversations (id), 
	FOREIGN KEY(message_id) REFERENCES messages (id)
);

CREATE INDEX ix_artifacts_conversation_id ON artifacts (conversation_id);

CREATE INDEX ix_artifacts_message_id ON artifacts (message_id);

CREATE TABLE artifact_versions (
	id VARCHAR NOT NULL, 
	artifact_id VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	title VARCHAR NOT NULL, 
	filename VARCHAR NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(artifact_id) REFERENCES artifacts (id) ON DELETE CASCADE
);

CREATE INDEX ix_artifact_versions_artifact_id ON artifact_versions (artifact_id);

CREATE UNIQUE INDEX ix_artifact_versions_artifact_version ON artifact_versions (artifact_id, version);


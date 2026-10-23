-- Frozen additive native FASTT pilot storage: Twin bridge control tables, writer markers and the
-- pilot admission ledger. No existing table is altered; nothing is dropped.

CREATE TABLE twin_bridge_inputs (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	provider_message_id BIGINT NOT NULL, 
	creator_slug VARCHAR NOT NULL, 
	activation_cutoff TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	bridge_scope_version INTEGER NOT NULL, 
	event_schema_version INTEGER NOT NULL, 
	conversation_id VARCHAR NOT NULL, 
	normalized_text TEXT NOT NULL, 
	event_json TEXT NOT NULL, 
	occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	state VARCHAR NOT NULL, 
	admitted_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	CONSTRAINT pk_twin_bridge_inputs PRIMARY KEY (account_id, fan_id, provider_message_id)
);

CREATE INDEX ix_twin_bridge_inputs_account_state_time ON twin_bridge_inputs (account_id, state, occurred_at, provider_message_id);

CREATE TABLE twin_bridge_turns (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	turn_id VARCHAR NOT NULL, 
	creator_slug VARCHAR NOT NULL, 
	conversation_id VARCHAR NOT NULL, 
	scope_fingerprint VARCHAR NOT NULL, 
	member_ids_json TEXT NOT NULL, 
	text TEXT NOT NULL, 
	event_json TEXT NOT NULL, 
	occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	received_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	state VARCHAR NOT NULL, 
	reason VARCHAR, 
	owner_token VARCHAR, 
	owner_expires_at TIMESTAMP WITHOUT TIME ZONE, 
	revision INTEGER NOT NULL, 
	request_id VARCHAR, 
	draft_text TEXT, 
	epoch INTEGER, 
	dispatch_revision INTEGER, 
	generation_attempts INTEGER NOT NULL, 
	status_polls INTEGER NOT NULL, 
	ack_attempts INTEGER NOT NULL, 
	cancel_attempts INTEGER NOT NULL, 
	next_action_at TIMESTAMP WITHOUT TIME ZONE, 
	provider_message_id VARCHAR, 
	delivered_at TIMESTAMP WITHOUT TIME ZONE, 
	committed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	CONSTRAINT pk_twin_bridge_turns PRIMARY KEY (account_id, fan_id, turn_id)
);

CREATE INDEX ix_twin_bridge_turns_conversation ON twin_bridge_turns (account_id, fan_id, created_at);

CREATE INDEX ix_twin_bridge_turns_state_next ON twin_bridge_turns (account_id, state, next_action_at);

CREATE TABLE twin_bridge_turn_members (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	provider_message_id BIGINT NOT NULL, 
	turn_id VARCHAR NOT NULL, 
	position INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	CONSTRAINT pk_twin_bridge_turn_members PRIMARY KEY (account_id, fan_id, provider_message_id)
);

CREATE INDEX ix_twin_bridge_turn_members_turn ON twin_bridge_turn_members (account_id, fan_id, turn_id, position);

CREATE TABLE twin_bridge_conversations (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	creator_slug VARCHAR NOT NULL, 
	conversation_id VARCHAR NOT NULL, 
	scope_fingerprint VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	reason VARCHAR, 
	revision INTEGER NOT NULL, 
	owner_token VARCHAR, 
	owner_expires_at TIMESTAMP WITHOUT TIME ZONE, 
	takeover_at TIMESTAMP WITHOUT TIME ZONE, 
	reset_at TIMESTAMP WITHOUT TIME ZONE, 
	cancelled_at TIMESTAMP WITHOUT TIME ZONE, 
	reserved BOOLEAN DEFAULT true NOT NULL, 
	send_budget_limit INTEGER NOT NULL, 
	send_budget_used INTEGER NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	CONSTRAINT pk_twin_bridge_conversations PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE twin_bridge_send_attempts (
	id SERIAL NOT NULL, 
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	turn_id VARCHAR NOT NULL, 
	attempt_no INTEGER NOT NULL, 
	state VARCHAR NOT NULL, 
	reason VARCHAR, 
	owner_token VARCHAR NOT NULL, 
	revision INTEGER NOT NULL, 
	provider_message_id VARCHAR, 
	evidence_json TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL, 
	CONSTRAINT pk_twin_bridge_send_attempts PRIMARY KEY (id), 
	CONSTRAINT uq_twin_bridge_send_attempt UNIQUE (account_id, fan_id, turn_id, attempt_no)
);

CREATE INDEX ix_twin_bridge_send_attempts_state ON twin_bridge_send_attempts (account_id, state, created_at);

CREATE TABLE twin_bridge_writes (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	holder VARCHAR NOT NULL, 
	expires_at VARCHAR NOT NULL, 
	state VARCHAR DEFAULT 'held' NOT NULL, 
	CONSTRAINT pk_twin_bridge_writes PRIMARY KEY (account_id, fan_id, holder)
);

CREATE TABLE native_pilots (
	pilot_id VARCHAR NOT NULL, 
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	owner_token VARCHAR NOT NULL, 
	cutoff VARCHAR NOT NULL, 
	deadline VARCHAR NOT NULL, 
	grant_limit INTEGER NOT NULL, 
	baseline_used INTEGER NOT NULL, 
	end_limit INTEGER NOT NULL, 
	revision INTEGER NOT NULL, 
	state VARCHAR NOT NULL, 
	reason VARCHAR, 
	model_calls INTEGER DEFAULT 0 NOT NULL, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_native_pilots PRIMARY KEY (pilot_id)
);

CREATE TABLE native_pilot_inputs (
	input_key VARCHAR NOT NULL, 
	pilot_id VARCHAR NOT NULL, 
	message_id BIGINT NOT NULL, 
	source_hash VARCHAR NOT NULL, 
	created_at VARCHAR NOT NULL, 
	ingested_at VARCHAR NOT NULL, 
	member_ids_json TEXT NOT NULL, 
	earliest_created_at VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	claimed_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_native_pilot_inputs PRIMARY KEY (input_key), 
	CONSTRAINT uq_native_pilot_inputs_message UNIQUE (pilot_id, message_id)
);

CREATE TABLE native_pilot_receipts (
	attempt_id INTEGER NOT NULL, 
	pilot_id VARCHAR NOT NULL, 
	input_key VARCHAR NOT NULL, 
	bubble_no INTEGER NOT NULL, 
	state VARCHAR NOT NULL, 
	provider_message_id VARCHAR, 
	response_json TEXT, 
	evidence_json TEXT, 
	created_at VARCHAR NOT NULL, 
	updated_at VARCHAR NOT NULL, 
	CONSTRAINT pk_native_pilot_receipts PRIMARY KEY (attempt_id), 
	CONSTRAINT uq_native_pilot_receipts_bubble UNIQUE (input_key, bubble_no)
);

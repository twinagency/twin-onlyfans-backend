-- FASTT runner: one new text-intent origin and a durable per-fan runtime owner marker.
-- Additive only. Nothing is enabled: a fan has no owner until an operator claims it for the runner.

ALTER TABLE of_text_intent DROP CONSTRAINT ck_of_text_intent_origin;
ALTER TABLE of_text_intent ADD CONSTRAINT ck_of_text_intent_origin CHECK (origin IN ('automated','manual','campaign','engagement','fastt'));

CREATE TABLE of_runtime_owner (
	account_id VARCHAR NOT NULL, 
	fan_id BIGINT NOT NULL, 
	owner VARCHAR(16) NOT NULL, 
	actor VARCHAR(128) NOT NULL, 
	claimed_at TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	released_at TIMESTAMP WITHOUT TIME ZONE, 
	released_by VARCHAR(128), 
	CONSTRAINT pk_of_runtime_owner PRIMARY KEY (account_id, fan_id), 
	CONSTRAINT fk_of_runtime_owner_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_of_runtime_owner_owner CHECK (owner IN ('fastt'))
);

-- Frozen canonical 0070 PostgreSQL baseline. Generated from b3a7c00 models with typed Boolean defaults.
-- Bootstrap only; never execute over an existing schema.
CREATE TABLE account_health (
	account_id VARCHAR NOT NULL,
	session_dead_at TIMESTAMP WITHOUT TIME ZONE,
	session_dead_reason VARCHAR,
	last_probe_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_account_health PRIMARY KEY (account_id)
);

CREATE TABLE accounts (
	id VARCHAR NOT NULL,
	nickname VARCHAR,
	color VARCHAR,
	proxy_label VARCHAR,
	x_of_rev VARCHAR,
	static_param VARCHAR,
	user_agent VARCHAR,
	is_active_default BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	last_used_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_accounts PRIMARY KEY (id)
);

CREATE TABLE app_errors (
	id SERIAL NOT NULL,
	occurred_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	source VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	message TEXT NOT NULL,
	stack TEXT,
	url TEXT,
	account_id VARCHAR,
	employee_id INTEGER,
	user_agent TEXT,
	context_json TEXT,
	CONSTRAINT pk_app_errors PRIMARY KEY (id)
);

CREATE TABLE audience_enrollments (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	status VARCHAR NOT NULL,
	first_seen_gen INTEGER,
	attempts INTEGER NOT NULL,
	next_retry_at TIMESTAMP WITHOUT TIME ZONE,
	reason VARCHAR,
	first_seen_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_audience_enrollments PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE automation_runs (
	id SERIAL NOT NULL,
	account_id VARCHAR,
	kind VARCHAR NOT NULL,
	started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	completed_at TIMESTAMP WITHOUT TIME ZONE,
	status VARCHAR NOT NULL,
	stats_json TEXT,
	error_text TEXT,
	CONSTRAINT pk_automation_runs PRIMARY KEY (id)
);

CREATE TABLE autoreply_state (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	spell_inbound_at TIMESTAMP WITHOUT TIME ZONE,
	nudges_sent INTEGER NOT NULL,
	last_nudge_at TIMESTAMP WITHOUT TIME ZONE,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_autoreply_state PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE blacklist (
	fan_id BIGSERIAL NOT NULL,
	reason VARCHAR,
	added_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_blacklist PRIMARY KEY (fan_id)
);

CREATE TABLE chatters (
	id VARCHAR NOT NULL,
	username VARCHAR NOT NULL,
	password VARCHAR NOT NULL,
	display_name VARCHAR,
	color VARCHAR,
	is_active BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	last_seen_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_chatters PRIMARY KEY (id),
	CONSTRAINT uq_chatters_username UNIQUE (username)
);

CREATE TABLE employees (
	id SERIAL NOT NULL,
	display_name VARCHAR NOT NULL,
	color VARCHAR,
	is_active BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	user_id VARCHAR,
	chatter_id VARCHAR,
	CONSTRAINT pk_employees PRIMARY KEY (id)
);

CREATE TABLE fan_lease (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	automation_kind VARCHAR NOT NULL,
	leased_until TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	acquired_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_fan_lease PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE fan_profiles (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	original_name VARCHAR,
	message_count_at_gen INTEGER NOT NULL,
	total_spend_at_gen_cents INTEGER NOT NULL,
	nickname VARCHAR,
	short_bio TEXT,
	bullet_points TEXT,
	q1 TEXT,
	q2 TEXT,
	q3 TEXT,
	tease1 TEXT,
	tease2 TEXT,
	tease3 TEXT,
	applied_notes TEXT,
	notes_applied_successfully BOOLEAN NOT NULL,
	last_applied_at TIMESTAMP WITHOUT TIME ZONE,
	last_applied_nickname VARCHAR,
	last_applied_notes TEXT,
	last_generated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	generated_by_grok_call_id INTEGER,
	CONSTRAINT pk_fan_profiles PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE follow_ping_state (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	last_pinged_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	ping_count INTEGER NOT NULL,
	CONSTRAINT pk_follow_ping_state PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE followup_state (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	phase VARCHAR NOT NULL,
	cycle INTEGER NOT NULL,
	silence_started_at TIMESTAMP WITHOUT TIME ZONE,
	fan_last_reply_at TIMESTAMP WITHOUT TIME ZONE,
	cooldown_until TIMESTAMP WITHOUT TIME ZONE,
	messages_sent TEXT NOT NULL,
	completed BOOLEAN NOT NULL,
	stopped_at TIMESTAMP WITHOUT TIME ZONE,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_followup_state PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE mass_message_funnels (
	id SERIAL NOT NULL,
	name VARCHAR NOT NULL,
	description TEXT,
	opening_message TEXT NOT NULL,
	vault_folder VARCHAR,
	media_indices TEXT NOT NULL,
	opening_vault_folder VARCHAR,
	opening_media_indices TEXT,
	steps_json TEXT NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_mass_message_funnels PRIMARY KEY (id),
	CONSTRAINT uq_mass_message_funnels_name UNIQUE (name)
);

CREATE TABLE model_pricing (
	model VARCHAR NOT NULL,
	provider VARCHAR NOT NULL,
	input_per_1k_cents NUMERIC(12, 6) DEFAULT 0 NOT NULL,
	output_per_1k_cents NUMERIC(12, 6) DEFAULT 0 NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_model_pricing PRIMARY KEY (model)
);

CREATE TABLE nudge_state (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	last_seen_online_at TIMESTAMP WITHOUT TIME ZONE,
	last_nudged_at TIMESTAMP WITHOUT TIME ZONE,
	nudge_count INTEGER NOT NULL,
	no_reply_streak INTEGER NOT NULL,
	pending_job_id INTEGER,
	last_variation_idx INTEGER,
	last_slot VARCHAR,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_nudge_state PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE perf_events (
	id SERIAL NOT NULL,
	tab_id VARCHAR NOT NULL,
	parent_tab_id VARCHAR,
	op_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	phase VARCHAR NOT NULL,
	client_ts_ms BIGINT NOT NULL,
	received_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	employee_id INTEGER,
	account_id VARCHAR,
	meta_json TEXT,
	CONSTRAINT pk_perf_events PRIMARY KEY (id)
);

CREATE TABLE quota_audit (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	day VARCHAR NOT NULL,
	reason VARCHAR NOT NULL,
	n INTEGER NOT NULL,
	enforced BOOLEAN NOT NULL,
	quota INTEGER NOT NULL,
	used INTEGER NOT NULL,
	runway_left INTEGER NOT NULL,
	wait_h FLOAT,
	dry_h FLOAT,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_quota_audit PRIMARY KEY (account_id, fan_id, day, reason, enforced)
);

CREATE TABLE scrape_history (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	last_message_id BIGINT,
	last_message_text TEXT,
	last_scrape_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_scrape_history PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE skip_list (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	reason VARCHAR,
	added_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_skip_list PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE tip_reward_log (
	account_id VARCHAR NOT NULL,
	tip_message_id BIGINT NOT NULL,
	fan_id BIGINT NOT NULL,
	tip_cents INTEGER NOT NULL,
	basis_cents INTEGER NOT NULL,
	tier_name VARCHAR,
	images_sent INTEGER NOT NULL,
	reward_message_id BIGINT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_tip_reward_log PRIMARY KEY (account_id, tip_message_id)
);

CREATE TABLE translation_cache (
	text_hash VARCHAR NOT NULL,
	target VARCHAR NOT NULL,
	src_lang VARCHAR NOT NULL,
	source_text TEXT NOT NULL,
	translated TEXT NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_translation_cache PRIMARY KEY (text_hash)
);

CREATE TABLE users (
	id VARCHAR NOT NULL,
	username VARCHAR NOT NULL,
	password VARCHAR NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	last_seen_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	is_admin BOOLEAN DEFAULT false NOT NULL,
	CONSTRAINT pk_users PRIMARY KEY (id),
	CONSTRAINT uq_users_username UNIQUE (username)
);

CREATE TABLE vault_presets (
	id SERIAL NOT NULL,
	name VARCHAR NOT NULL,
	folder_name VARCHAR NOT NULL,
	index_in_folder INTEGER,
	index_strategy VARCHAR NOT NULL,
	notes TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_vault_presets PRIMARY KEY (id),
	CONSTRAINT uq_vault_presets_name UNIQUE (name)
);

CREATE TABLE vault_sends (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	media_id BIGINT NOT NULL,
	message_id BIGINT,
	price_cents INTEGER NOT NULL,
	sent_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	was_purchased BOOLEAN,
	CONSTRAINT pk_vault_sends PRIMARY KEY (id)
);

CREATE TABLE welcome_sent (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	fan_username VARCHAR,
	sent_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_welcome_sent PRIMARY KEY (account_id, fan_id)
);

CREATE TABLE account_ai_config (
	account_id VARCHAR NOT NULL,
	persona TEXT,
	welcome_rules TEXT,
	utc_offset INTEGER NOT NULL,
	timezone VARCHAR,
	location VARCHAR,
	persona_facts_json TEXT,
	language VARCHAR,
	voice VARCHAR,
	time_activities_json TEXT,
	day_log_json TEXT,
	time_images_json TEXT,
	welcome_pinned_json TEXT,
	daily_cost_cap_cents INTEGER NOT NULL,
	model VARCHAR,
	model_by_purpose TEXT,
	reasoning_effort VARCHAR,
	nudge_config_json TEXT,
	webhook_config_json TEXT,
	autoreply_config_json TEXT,
	style_config_json TEXT,
	tip_reward_config_json TEXT,
	ai_chatter_config_json TEXT,
	ppv_library_config_json TEXT,
	vault_ai_config_json TEXT,
	make_right_config_json TEXT,
	audience_mode VARCHAR,
	audience_list_id INTEGER,
	audience_auto_add BOOLEAN,
	banned_words_config_json TEXT,
	cotag_tag_videos BOOLEAN,
	cotag_username VARCHAR,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_account_ai_config PRIMARY KEY (account_id),
	CONSTRAINT fk_account_ai_config_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE actions (
	id SERIAL NOT NULL,
	employee_id INTEGER,
	account_id VARCHAR,
	action VARCHAR NOT NULL,
	target_type VARCHAR,
	target_id VARCHAR,
	payload_json TEXT,
	at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_actions PRIMARY KEY (id),
	CONSTRAINT fk_actions_employee_id_employees FOREIGN KEY(employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE automation_rules (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	trigger_json TEXT NOT NULL,
	steps_json TEXT NOT NULL,
	is_enabled BOOLEAN NOT NULL,
	quiet_hours_json TEXT,
	frequency_caps_json TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_automation_rules PRIMARY KEY (id),
	CONSTRAINT fk_automation_rules_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE catalog_scripts (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	theme TEXT,
	status VARCHAR NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_catalog_scripts PRIMARY KEY (id),
	CONSTRAINT fk_catalog_scripts_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE chats (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	last_message_id BIGINT,
	last_message_at TIMESTAMP WITHOUT TIME ZONE,
	last_message_preview TEXT,
	unread_count INTEGER NOT NULL,
	is_pinned BOOLEAN NOT NULL,
	is_priority BOOLEAN NOT NULL,
	hidden_locally BOOLEAN NOT NULL,
	list_ids TEXT NOT NULL,
	preview_updated_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_chats PRIMARY KEY (account_id, fan_id),
	CONSTRAINT fk_chats_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE chatter_account_access (
	chatter_id VARCHAR NOT NULL,
	user_id VARCHAR NOT NULL,
	account_id VARCHAR NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_chatter_account_access PRIMARY KEY (chatter_id, user_id, account_id),
	CONSTRAINT fk_chatter_account_access_chatter_id_chatters FOREIGN KEY(chatter_id) REFERENCES chatters (id) ON DELETE CASCADE,
	CONSTRAINT fk_chatter_account_access_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	CONSTRAINT fk_chatter_account_access_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE chatter_folder_access (
	chatter_id VARCHAR NOT NULL,
	account_id VARCHAR NOT NULL,
	folder_id BIGINT NOT NULL,
	folder_name VARCHAR,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_chatter_folder_access PRIMARY KEY (chatter_id, account_id, folder_id),
	CONSTRAINT fk_chatter_folder_access_chatter_id_chatters FOREIGN KEY(chatter_id) REFERENCES chatters (id) ON DELETE CASCADE,
	CONSTRAINT fk_chatter_folder_access_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE chatter_users (
	chatter_id VARCHAR NOT NULL,
	user_id VARCHAR NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_chatter_users PRIMARY KEY (chatter_id, user_id),
	CONSTRAINT fk_chatter_users_chatter_id_chatters FOREIGN KEY(chatter_id) REFERENCES chatters (id) ON DELETE CASCADE,
	CONSTRAINT fk_chatter_users_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE chatters_invites (
	token VARCHAR NOT NULL,
	user_id VARCHAR NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	expires_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	used_at TIMESTAMP WITHOUT TIME ZONE,
	used_by_chatter_id VARCHAR,
	CONSTRAINT pk_chatters_invites PRIMARY KEY (token),
	CONSTRAINT fk_chatters_invites_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE employee_account_access (
	employee_id INTEGER NOT NULL,
	account_id VARCHAR,
	CONSTRAINT pk_employee_account_access PRIMARY KEY (employee_id, account_id),
	CONSTRAINT fk_employee_account_access_employee_id_employees FOREIGN KEY(employee_id) REFERENCES employees (id) ON DELETE CASCADE,
	CONSTRAINT fk_employee_account_access_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE event_inbox (
	id SERIAL NOT NULL,
	account_id VARCHAR,
	source VARCHAR NOT NULL,
	provider_event_id VARCHAR,
	event_type VARCHAR NOT NULL,
	payload_json TEXT NOT NULL,
	received_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	processed_at TIMESTAMP WITHOUT TIME ZONE,
	processed_status VARCHAR,
	processed_error TEXT,
	CONSTRAINT pk_event_inbox PRIMARY KEY (id),
	CONSTRAINT fk_event_inbox_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE fans (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	of_username VARCHAR,
	of_display_name VARCHAR,
	avatar_url TEXT,
	custom_nickname VARCHAR,
	generated_nickname VARCHAR,
	fan_chosen_nickname VARCHAR,
	real_name VARCHAR,
	is_name_real BOOLEAN NOT NULL,
	his_age VARCHAR,
	home_country VARCHAR,
	home_city VARCHAR,
	hobbies TEXT,
	fetishes TEXT,
	recent_events TEXT NOT NULL,
	recent_events_timeline TEXT,
	self_description TEXT,
	description TEXT,
	likes_boobs BOOLEAN NOT NULL,
	likes_ass BOOLEAN NOT NULL,
	timezone VARCHAR,
	language VARCHAR,
	language_source VARCHAR,
	occupation TEXT,
	employer TEXT,
	relationship_status TEXT,
	partner_name TEXT,
	has_kids BOOLEAN,
	family_names TEXT NOT NULL,
	pets TEXT NOT NULL,
	mood_at_last_message TEXT,
	sentiment_trend TEXT,
	relationship_stage TEXT,
	communication_style TEXT NOT NULL,
	automation_paused_until TIMESTAMP WITHOUT TIME ZONE,
	questions_asked TEXT,
	persona_claims_json TEXT,
	persona_age_claimed TEXT,
	persona_location_claimed TEXT,
	persona_job_claimed TEXT,
	profile_last_synced_at TIMESTAMP WITHOUT TIME ZONE,
	grok_facts_updated_at TIMESTAMP WITHOUT TIME ZONE,
	notes TEXT,
	applied_notes TEXT,
	applied_notes_at TIMESTAMP WITHOUT TIME ZONE,
	customs_owed_at TIMESTAMP WITHOUT TIME ZONE,
	customs_cleared_at TIMESTAMP WITHOUT TIME ZONE,
	gather_close_at TIMESTAMP WITHOUT TIME ZONE,
	tags TEXT NOT NULL,
	custom_fields TEXT NOT NULL,
	lifetime_spend_cents BIGINT NOT NULL,
	total_likes INTEGER,
	is_bot BOOLEAN NOT NULL,
	turn_counter INTEGER NOT NULL,
	last_message_received_at TIMESTAMP WITHOUT TIME ZONE,
	last_message_sent_at TIMESTAMP WITHOUT TIME ZONE,
	last_online_at TIMESTAMP WITHOUT TIME ZONE,
	subscribed_at TIMESTAMP WITHOUT TIME ZONE,
	subscription_expires_at TIMESTAMP WITHOUT TIME ZONE,
	subscription_status VARCHAR,
	is_followed BOOLEAN NOT NULL,
	follow_examined_at TIMESTAMP WITHOUT TIME ZONE,
	is_muted BOOLEAN DEFAULT false NOT NULL,
	joined_date VARCHAR,
	deep_convo_state VARCHAR NOT NULL,
	deep_convo_skip_level INTEGER NOT NULL,
	deep_convo_skip_remaining INTEGER NOT NULL,
	deep_convo_updated_at TIMESTAMP WITHOUT TIME ZONE,
	deep_convo_q_text TEXT,
	deep_convo_tease_text TEXT,
	source VARCHAR NOT NULL,
	raw_json TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_fans PRIMARY KEY (account_id, fan_id),
	CONSTRAINT fk_fans_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE funnel_account_media (
	funnel_id INTEGER NOT NULL,
	account_id VARCHAR NOT NULL,
	opening_media_ids TEXT NOT NULL,
	steps_media_json TEXT NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_funnel_account_media PRIMARY KEY (funnel_id, account_id),
	CONSTRAINT fk_funnel_account_media_funnel_id_mass_message_funnels FOREIGN KEY(funnel_id) REFERENCES mass_message_funnels (id) ON DELETE CASCADE,
	CONSTRAINT fk_funnel_account_media_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id)
);

CREATE TABLE funnel_responders (
	account_id VARCHAR NOT NULL,
	funnel_id INTEGER NOT NULL,
	fan_id BIGINT NOT NULL,
	mass_run_id INTEGER,
	first_replied_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_funnel_responders PRIMARY KEY (account_id, funnel_id, fan_id),
	CONSTRAINT fk_funnel_responders_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
	CONSTRAINT fk_funnel_responders_funnel_id_mass_message_funnels FOREIGN KEY(funnel_id) REFERENCES mass_message_funnels (id) ON DELETE CASCADE
);

CREATE TABLE grok_daily_cost (
	day VARCHAR NOT NULL,
	account_id VARCHAR,
	provider VARCHAR DEFAULT 'grok' NOT NULL,
	cost_cents INTEGER NOT NULL,
	call_count INTEGER NOT NULL,
	is_capped BOOLEAN NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_grok_daily_cost PRIMARY KEY (day, account_id, provider),
	CONSTRAINT fk_grok_daily_cost_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE ladder_quote (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	rung_index INTEGER NOT NULL,
	media_key VARCHAR NOT NULL,
	item_id INTEGER,
	band_lo INTEGER NOT NULL,
	band_hi INTEGER NOT NULL,
	base_cents INTEGER NOT NULL,
	arm_mult FLOAT NOT NULL,
	price_cents INTEGER NOT NULL,
	pre_clamp_cents INTEGER NOT NULL,
	clamped_by VARCHAR,
	kind VARCHAR NOT NULL,
	message_id BIGINT,
	sent_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	paid BOOLEAN NOT NULL,
	paid_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_ladder_quote PRIMARY KEY (id),
	CONSTRAINT fk_ladder_quote_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE ladder_state (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	status VARCHAR NOT NULL,
	rung_index INTEGER NOT NULL,
	last_ask_at TIMESTAMP WITHOUT TIME ZONE,
	last_paid_at TIMESTAMP WITHOUT TIME ZONE,
	session_idle_at TIMESTAMP WITHOUT TIME ZONE,
	offers_paused_until TIMESTAMP WITHOUT TIME ZONE,
	daily_ask_cents INTEGER NOT NULL,
	daily_day VARCHAR,
	hot_until TIMESTAMP WITHOUT TIME ZONE,
	unpaid_rungs INTEGER NOT NULL,
	objection_at TIMESTAMP WITHOUT TIME ZONE,
	discount_count INTEGER NOT NULL,
	bot_accused_count INTEGER NOT NULL,
	companion_until TIMESTAMP WITHOUT TIME ZONE,
	cooldown_until TIMESTAMP WITHOUT TIME ZONE,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_ladder_state PRIMARY KEY (account_id, fan_id),
	CONSTRAINT fk_ladder_state_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE lists (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	of_list_id BIGINT,
	name VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	query_json TEXT,
	is_system BOOLEAN NOT NULL,
	synced_at TIMESTAMP WITHOUT TIME ZONE,
	sync_meta_json TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_lists PRIMARY KEY (id),
	CONSTRAINT fk_lists_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE mass_broadcast_cache (
	account_id VARCHAR NOT NULL,
	queue_id BIGINT NOT NULL,
	sent_at TIMESTAMP WITHOUT TIME ZONE,
	body_text TEXT,
	raw_text TEXT,
	giphy_id VARCHAR,
	is_free BOOLEAN DEFAULT true NOT NULL,
	is_tip BOOLEAN DEFAULT false NOT NULL,
	media_count INTEGER DEFAULT 0 NOT NULL,
	media_json TEXT,
	previews_json TEXT,
	sent_count INTEGER DEFAULT 0 NOT NULL,
	viewed_count INTEGER DEFAULT 0 NOT NULL,
	is_canceled BOOLEAN DEFAULT false NOT NULL,
	can_unsend BOOLEAN DEFAULT true NOT NULL,
	unsend_seconds INTEGER,
	template VARCHAR,
	fetched_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_mass_broadcast_cache PRIMARY KEY (account_id, queue_id),
	CONSTRAINT fk_mass_broadcast_cache_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE mass_runs (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	funnel_id INTEGER,
	started_by_employee_id INTEGER,
	audience_filter TEXT NOT NULL,
	recipient_count INTEGER NOT NULL,
	queue_id BIGINT,
	automation_kind VARCHAR,
	started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	completed_at TIMESTAMP WITHOUT TIME ZONE,
	status VARCHAR NOT NULL,
	discovery_closed_at TIMESTAMP WITHOUT TIME ZONE,
	body TEXT,
	sent_at TIMESTAMP WITHOUT TIME ZONE,
	unsent_at TIMESTAMP WITHOUT TIME ZONE,
	recipients_json TEXT,
	CONSTRAINT pk_mass_runs PRIMARY KEY (id),
	CONSTRAINT fk_mass_runs_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id),
	CONSTRAINT fk_mass_runs_funnel_id_mass_message_funnels FOREIGN KEY(funnel_id) REFERENCES mass_message_funnels (id),
	CONSTRAINT fk_mass_runs_started_by_employee_id_employees FOREIGN KEY(started_by_employee_id) REFERENCES employees (id)
);

CREATE TABLE message_flags (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	message_id BIGINT NOT NULL,
	flagged_by_employee_id INTEGER NOT NULL,
	flagged_unread BOOLEAN NOT NULL,
	starred BOOLEAN NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_message_flags PRIMARY KEY (account_id, fan_id, message_id, flagged_by_employee_id),
	CONSTRAINT fk_message_flags_flagged_by_employee_id_employees FOREIGN KEY(flagged_by_employee_id) REFERENCES employees (id) ON DELETE CASCADE
);

CREATE TABLE of_creator_bindings (
	id VARCHAR NOT NULL,
	tenant_id VARCHAR NOT NULL,
	creator_id VARCHAR NOT NULL,
	creator_name VARCHAR NOT NULL,
	account_id VARCHAR NOT NULL,
	handle VARCHAR NOT NULL,
	channel VARCHAR NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	revoked_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_of_creator_bindings PRIMARY KEY (id),
	CONSTRAINT fk_of_creator_bindings_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE pending_offer (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	media_key VARCHAR,
	item_id INTEGER,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	expires_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_pending_offer PRIMARY KEY (account_id, fan_id),
	CONSTRAINT fk_pending_offer_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE posts (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	of_post_id BIGINT,
	temp_id VARCHAR,
	status VARCHAR NOT NULL,
	text TEXT,
	price_cents INTEGER NOT NULL,
	media_ids TEXT NOT NULL,
	label_ids TEXT NOT NULL,
	scheduled_for TIMESTAMP WITHOUT TIME ZONE,
	posted_at TIMESTAMP WITHOUT TIME ZONE,
	failed_reason TEXT,
	excluded_list_ids TEXT NOT NULL,
	created_by_employee_id INTEGER,
	raw_json TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_posts PRIMARY KEY (id),
	CONSTRAINT fk_posts_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
	CONSTRAINT fk_posts_created_by_employee_id_employees FOREIGN KEY(created_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE promo_campaigns (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	of_promo_id BIGINT,
	name VARCHAR NOT NULL,
	discount_percent INTEGER NOT NULL,
	price_cents INTEGER NOT NULL,
	subscribe_counts INTEGER NOT NULL,
	subscribe_days INTEGER NOT NULL,
	message TEXT,
	promo_type VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	subscriber_count INTEGER NOT NULL,
	auto_reactivate BOOLEAN NOT NULL,
	reactivate_count INTEGER NOT NULL,
	last_reactivated_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_promo_campaigns PRIMARY KEY (id),
	CONSTRAINT fk_promo_campaigns_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE prompts (
	id SERIAL NOT NULL,
	name VARCHAR NOT NULL,
	purpose VARCHAR NOT NULL,
	account_id VARCHAR,
	version INTEGER NOT NULL,
	body TEXT NOT NULL,
	variables_json TEXT NOT NULL,
	response_schema_json TEXT,
	is_active BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	created_by_employee_id INTEGER,
	CONSTRAINT pk_prompts PRIMARY KEY (id),
	CONSTRAINT uq_prompts_name_account_version UNIQUE (name, account_id, version),
	CONSTRAINT fk_prompts_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
	CONSTRAINT fk_prompts_created_by_employee_id_employees FOREIGN KEY(created_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE proxies (
	label VARCHAR NOT NULL,
	scheme VARCHAR NOT NULL,
	host VARCHAR NOT NULL,
	port INTEGER NOT NULL,
	username VARCHAR,
	password_encrypted TEXT,
	notes TEXT,
	verified_ip VARCHAR,
	verified_at TIMESTAMP WITHOUT TIME ZONE,
	assigned_account_id VARCHAR,
	CONSTRAINT pk_proxies PRIMARY KEY (label),
	CONSTRAINT fk_proxies_assigned_account_id_accounts FOREIGN KEY(assigned_account_id) REFERENCES accounts (id) ON DELETE SET NULL
);

CREATE TABLE resolution_log (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	incident_key VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	detected_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	status VARCHAR DEFAULT 'detected' NOT NULL,
	remediation_json TEXT,
	resolved_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_resolution_log PRIMARY KEY (id),
	CONSTRAINT uq_resolution_incident UNIQUE (account_id, incident_key),
	CONSTRAINT fk_resolution_log_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE rhythm_state (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	context VARCHAR NOT NULL,
	wake_at TIMESTAMP WITHOUT TIME ZONE,
	deferrals INTEGER NOT NULL,
	last_cover_at TIMESTAMP WITHOUT TIME ZONE,
	recent_turns_json TEXT,
	ghost_anchor TIMESTAMP WITHOUT TIME ZONE,
	stepout_mark INTEGER,
	stepout_target INTEGER,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_rhythm_state PRIMARY KEY (account_id, fan_id),
	CONSTRAINT fk_rhythm_state_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE saved_replies (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	title VARCHAR,
	text TEXT NOT NULL,
	price_cents INTEGER NOT NULL,
	locked_text BOOLEAN NOT NULL,
	media_json TEXT NOT NULL,
	tagged_users_json TEXT NOT NULL,
	previews_json TEXT NOT NULL,
	gif_id VARCHAR,
	gif_url TEXT,
	script_id VARCHAR,
	script_step INTEGER,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_saved_replies PRIMARY KEY (id),
	CONSTRAINT fk_saved_replies_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE sessions (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	captured_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	cookies_json TEXT NOT NULL,
	x_of_rev VARCHAR NOT NULL,
	signing_rules_json TEXT NOT NULL,
	is_latest BOOLEAN NOT NULL,
	CONSTRAINT pk_sessions PRIMARY KEY (id),
	CONSTRAINT fk_sessions_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE shortcuts (
	id SERIAL NOT NULL,
	employee_id INTEGER NOT NULL,
	kind VARCHAR NOT NULL,
	trigger VARCHAR,
	body TEXT,
	media_ids TEXT NOT NULL,
	price_cents INTEGER,
	hotkey VARCHAR,
	position INTEGER NOT NULL,
	CONSTRAINT pk_shortcuts PRIMARY KEY (id),
	CONSTRAINT fk_shortcuts_employee_id_employees FOREIGN KEY(employee_id) REFERENCES employees (id) ON DELETE CASCADE
);

CREATE TABLE smart_lists (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	rules_json TEXT NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_smart_lists PRIMARY KEY (id),
	CONSTRAINT uq_smart_lists_account_name UNIQUE (account_id, name),
	CONSTRAINT fk_smart_lists_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE tracking_links (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	slug VARCHAR NOT NULL,
	target_url TEXT NOT NULL,
	click_count INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_tracking_links PRIMARY KEY (id),
	CONSTRAINT uq_tracking_links_slug UNIQUE (slug),
	CONSTRAINT fk_tracking_links_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE transaction_scan_history (
	account_id VARCHAR NOT NULL,
	last_scan_at TIMESTAMP WITHOUT TIME ZONE,
	last_marker INTEGER,
	oldest_seen_occurred_at TIMESTAMP WITHOUT TIME ZONE,
	newest_seen_occurred_at TIMESTAMP WITHOUT TIME ZONE,
	fully_backfilled BOOLEAN DEFAULT false NOT NULL,
	backfill_floor TIMESTAMP WITHOUT TIME ZONE,
	current_status VARCHAR DEFAULT 'idle' NOT NULL,
	last_error VARCHAR,
	consecutive_failures INTEGER DEFAULT 0 NOT NULL,
	paused_until TIMESTAMP WITHOUT TIME ZONE,
	rows_inserted_total INTEGER DEFAULT 0 NOT NULL,
	rows_patched_total INTEGER DEFAULT 0 NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_transaction_scan_history PRIMARY KEY (account_id),
	CONSTRAINT fk_transaction_scan_history_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE transactions (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	fan_id BIGINT,
	kind VARCHAR NOT NULL,
	message_id BIGINT,
	amount_cents INTEGER NOT NULL,
	currency VARCHAR NOT NULL,
	occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	raw_json TEXT,
	ingested_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	provider_transaction_id VARCHAR(64),
	status VARCHAR(24) DEFAULT 'cleared' NOT NULL,
	cleared_at TIMESTAMP WITHOUT TIME ZONE,
	payout_pending_days INTEGER,
	net_cents INTEGER,
	fee_cents INTEGER,
	vat_cents INTEGER,
	description TEXT,
	source VARCHAR(8) DEFAULT 'ws' NOT NULL,
	attributed_employee_id INTEGER,
	CONSTRAINT pk_transactions PRIMARY KEY (id),
	CONSTRAINT fk_transactions_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE trial_link_mirrors (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	of_trial_id BIGINT,
	name VARCHAR NOT NULL,
	subscribe_days INTEGER NOT NULL,
	subscribe_counts INTEGER NOT NULL,
	url TEXT,
	claim_counts INTEGER NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_trial_link_mirrors PRIMARY KEY (id),
	CONSTRAINT fk_trial_link_mirrors_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE user_accounts (
	user_id VARCHAR NOT NULL,
	account_id VARCHAR NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_user_accounts PRIMARY KEY (user_id, account_id),
	CONSTRAINT fk_user_accounts_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE,
	CONSTRAINT fk_user_accounts_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE user_llm_keys (
	user_id VARCHAR NOT NULL,
	provider VARCHAR NOT NULL,
	api_key TEXT NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_user_llm_keys PRIMARY KEY (user_id, provider),
	CONSTRAINT fk_user_llm_keys_user_id_users FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE vault_ai_review_items (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	payload_json TEXT NOT NULL,
	baseline_json TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	resolved_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_vault_ai_review_items PRIMARY KEY (id),
	CONSTRAINT fk_vault_ai_review_items_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE vault_cache_runs (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	status VARCHAR NOT NULL,
	phase VARCHAR,
	total_seen INTEGER NOT NULL,
	upserted INTEGER NOT NULL,
	pages_done INTEGER NOT NULL,
	error TEXT,
	started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	finished_at TIMESTAMP WITHOUT TIME ZONE,
	CONSTRAINT pk_vault_cache_runs PRIMARY KEY (id),
	CONSTRAINT fk_vault_cache_runs_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE vault_daily_usage (
	account_id VARCHAR NOT NULL,
	media_id BIGINT NOT NULL,
	sent_on DATE NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_vault_daily_usage PRIMARY KEY (account_id, media_id, sent_on),
	CONSTRAINT fk_vault_daily_usage_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE vault_folder_items (
	account_id VARCHAR NOT NULL,
	folder_id INTEGER NOT NULL,
	media_id BIGINT NOT NULL,
	manual_order INTEGER,
	added_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_vault_folder_items PRIMARY KEY (account_id, folder_id, media_id),
	CONSTRAINT fk_vault_folder_items_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE vault_folders (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	of_list_id BIGINT,
	created_by VARCHAR NOT NULL,
	deleted_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_vault_folders PRIMARY KEY (id),
	CONSTRAINT fk_vault_folders_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE vault_items (
	account_id VARCHAR NOT NULL,
	media_id BIGINT NOT NULL,
	kind VARCHAR NOT NULL,
	duration_seconds INTEGER,
	width INTEGER,
	height INTEGER,
	thumb_url TEXT,
	full_url TEXT,
	folder_id BIGINT,
	description TEXT,
	suggested_price_cents INTEGER,
	default_price_cents INTEGER,
	tags TEXT NOT NULL,
	notes TEXT,
	send_count INTEGER NOT NULL,
	last_sent_at TIMESTAMP WITHOUT TIME ZONE,
	raw_json TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	search_text TEXT,
	of_terms TEXT,
	of_folder_ids TEXT,
	content_hash VARCHAR,
	content_hash_kind VARCHAR,
	updated_at_of VARCHAR,
	last_seen_run_id BIGINT,
	removed_at TIMESTAMP WITHOUT TIME ZONE,
	updated_at TIMESTAMP WITHOUT TIME ZONE,
	manual_order INTEGER,
	video_description TEXT,
	explicitness_tier VARCHAR,
	story_suitable BOOLEAN,
	story_score INTEGER,
	tip_vault_flag BOOLEAN,
	tip_vault_score INTEGER,
	suggested_caption TEXT,
	suggested_script TEXT,
	describe_status VARCHAR,
	describe_generated_at TIMESTAMP WITHOUT TIME ZONE,
	describe_model VARCHAR,
	describe_call_id BIGINT,
	frames_sampled INTEGER,
	ai_fields_json TEXT,
	operator_overrides_json TEXT,
	locked_fields_json TEXT,
	review_state VARCHAR,
	reviewed_at TIMESTAMP WITHOUT TIME ZONE,
	reviewed_by VARCHAR,
	script_id BIGINT,
	script_seq INTEGER,
	script_score INTEGER,
	script_reversed BOOLEAN,
	CONSTRAINT pk_vault_items PRIMARY KEY (account_id, media_id),
	CONSTRAINT fk_vault_items_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE vault_of_query_log (
	account_id VARCHAR NOT NULL,
	query VARCHAR NOT NULL,
	match_count INTEGER NOT NULL,
	fetched_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_vault_of_query_log PRIMARY KEY (account_id, query),
	CONSTRAINT fk_vault_of_query_log_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE vault_response_cache (
	account_id VARCHAR NOT NULL,
	query_key VARCHAR NOT NULL,
	response_json TEXT NOT NULL,
	fetched_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_vault_response_cache PRIMARY KEY (account_id, query_key),
	CONSTRAINT fk_vault_response_cache_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE wall_media (
	account_id VARCHAR NOT NULL,
	media_id BIGINT NOT NULL,
	post_id BIGINT,
	post_published_at TIMESTAMP WITHOUT TIME ZONE,
	price_cents INTEGER DEFAULT '0' NOT NULL,
	first_seen_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_wall_media PRIMARY KEY (account_id, media_id),
	CONSTRAINT fk_wall_media_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE wall_scan_state (
	account_id VARCHAR NOT NULL,
	newest_post_published_at TIMESTAMP WITHOUT TIME ZONE,
	oldest_post_published_at TIMESTAMP WITHOUT TIME ZONE,
	fully_backfilled BOOLEAN NOT NULL,
	last_scan_at TIMESTAMP WITHOUT TIME ZONE,
	scanned_posts_total INTEGER NOT NULL,
	CONSTRAINT pk_wall_scan_state PRIMARY KEY (account_id),
	CONSTRAINT fk_wall_scan_state_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE
);

CREATE TABLE catalog_items (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	script_id INTEGER,
	position INTEGER,
	kind VARCHAR NOT NULL,
	label VARCHAR,
	description_for_ai TEXT,
	media_ids TEXT NOT NULL,
	preview_media_ids TEXT NOT NULL,
	duration_sec INTEGER,
	band_lo INTEGER,
	band_hi INTEGER,
	price_cents INTEGER NOT NULL,
	tip_unlock_cents INTEGER NOT NULL,
	is_free_teaser BOOLEAN NOT NULL,
	tags TEXT NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_catalog_items PRIMARY KEY (id),
	CONSTRAINT fk_catalog_items_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
	CONSTRAINT fk_catalog_items_script_id_catalog_scripts FOREIGN KEY(script_id) REFERENCES catalog_scripts (id) ON DELETE CASCADE
);

CREATE TABLE catalog_progress (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	script_id INTEGER NOT NULL,
	position INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	started_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_catalog_progress PRIMARY KEY (account_id, fan_id, script_id),
	CONSTRAINT fk_catalog_progress_script_id_catalog_scripts FOREIGN KEY(script_id) REFERENCES catalog_scripts (id) ON DELETE CASCADE
);

CREATE TABLE fan_safety_flags (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	flag_type VARCHAR NOT NULL,
	severity VARCHAR NOT NULL,
	details TEXT,
	flagged_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	flagged_by VARCHAR NOT NULL,
	flagged_by_employee_id INTEGER,
	resolved_at TIMESTAMP WITHOUT TIME ZONE,
	resolution VARCHAR,
	CONSTRAINT pk_fan_safety_flags PRIMARY KEY (id),
	CONSTRAINT fk_fan_safety_flags_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE CASCADE,
	CONSTRAINT fk_fan_safety_flags_flagged_by_employee_id_employees FOREIGN KEY(flagged_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE fan_triggers (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	trigger_phrase TEXT NOT NULL,
	severity VARCHAR NOT NULL,
	reason TEXT,
	flagged_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	flagged_by_employee_id INTEGER,
	CONSTRAINT pk_fan_triggers PRIMARY KEY (id),
	CONSTRAINT fk_fan_triggers_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE CASCADE,
	CONSTRAINT fk_fan_triggers_flagged_by_employee_id_employees FOREIGN KEY(flagged_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE funnel_state (
	mass_run_id INTEGER NOT NULL,
	fan_id BIGINT NOT NULL,
	current_step INTEGER NOT NULL,
	next_check_at TIMESTAMP WITHOUT TIME ZONE,
	check_count INTEGER NOT NULL,
	status VARCHAR NOT NULL,
	last_error TEXT,
	last_ppv_sent_at TIMESTAMP WITHOUT TIME ZONE,
	hops INTEGER,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_funnel_state PRIMARY KEY (mass_run_id, fan_id),
	CONSTRAINT fk_funnel_state_mass_run_id_mass_runs FOREIGN KEY(mass_run_id) REFERENCES mass_runs (id) ON DELETE CASCADE
);

CREATE TABLE grok_calls (
	id SERIAL NOT NULL,
	purpose VARCHAR NOT NULL,
	account_id VARCHAR,
	fan_id BIGINT,
	model VARCHAR NOT NULL,
	endpoint VARCHAR NOT NULL,
	temperature NUMERIC(4, 2),
	prompt_id INTEGER,
	prompt_json TEXT NOT NULL,
	response_json TEXT,
	response_text TEXT,
	latency_ms INTEGER,
	tokens_in INTEGER,
	tokens_out INTEGER,
	cost_cents INTEGER,
	provider VARCHAR DEFAULT 'grok' NOT NULL,
	cache_hit_tokens INTEGER,
	status VARCHAR DEFAULT 'pending' NOT NULL,
	was_dry_run BOOLEAN NOT NULL,
	error_text TEXT,
	called_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_grok_calls PRIMARY KEY (id),
	CONSTRAINT fk_grok_calls_prompt_id_prompts FOREIGN KEY(prompt_id) REFERENCES prompts (id) ON DELETE SET NULL
);

CREATE TABLE list_members (
	list_id INTEGER NOT NULL,
	fan_id BIGINT NOT NULL,
	added_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	added_by_employee_id INTEGER,
	CONSTRAINT pk_list_members PRIMARY KEY (list_id, fan_id),
	CONSTRAINT fk_list_members_list_id_lists FOREIGN KEY(list_id) REFERENCES lists (id) ON DELETE CASCADE,
	CONSTRAINT fk_list_members_added_by_employee_id_employees FOREIGN KEY(added_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE messages (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	message_id BIGINT NOT NULL,
	direction VARCHAR NOT NULL,
	sender_name VARCHAR NOT NULL,
	body TEXT NOT NULL,
	media_ids TEXT NOT NULL,
	media_count INTEGER NOT NULL,
	price_cents INTEGER NOT NULL,
	is_paid BOOLEAN,
	is_tip BOOLEAN NOT NULL,
	purchased_at TIMESTAMP WITHOUT TIME ZONE,
	is_unsent BOOLEAN NOT NULL,
	unsent_by_employee_id INTEGER,
	unsent_reason TEXT,
	unsent_at TIMESTAMP WITHOUT TIME ZONE,
	is_promise BOOLEAN NOT NULL,
	promise_due_date TIMESTAMP WITHOUT TIME ZONE,
	sent_by_employee_id INTEGER,
	temp_id VARCHAR,
	mass_run_id INTEGER,
	funnel_step INTEGER,
	automation_kind VARCHAR,
	raw_json TEXT,
	image_desc TEXT,
	created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	ingested_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_messages PRIMARY KEY (account_id, fan_id, message_id),
	CONSTRAINT fk_messages_mass_run_id_mass_runs FOREIGN KEY(mass_run_id) REFERENCES mass_runs (id) ON DELETE SET NULL,
	CONSTRAINT fk_messages_unsent_by_employee_id_employees FOREIGN KEY(unsent_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL,
	CONSTRAINT fk_messages_sent_by_employee_id_employees FOREIGN KEY(sent_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE of_conversation_state (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	mode VARCHAR DEFAULT 'automated' NOT NULL,
	revision INTEGER DEFAULT 0 NOT NULL,
	takeover_state VARCHAR DEFAULT 'none' NOT NULL,
	unresolved_attempt_id VARCHAR,
	requires_attention BOOLEAN DEFAULT false NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_of_conversation_state PRIMARY KEY (account_id, fan_id),
	CONSTRAINT fk_of_conversation_state_account_id_fans FOREIGN KEY(account_id, fan_id) REFERENCES fans (account_id, fan_id) ON DELETE CASCADE
);

CREATE TABLE scheduled_jobs (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	kind VARCHAR NOT NULL,
	payload_json TEXT NOT NULL,
	run_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	status VARCHAR NOT NULL,
	attempts INTEGER NOT NULL,
	last_error TEXT,
	rule_id INTEGER,
	created_by_employee_id INTEGER,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_scheduled_jobs PRIMARY KEY (id),
	CONSTRAINT fk_scheduled_jobs_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
	CONSTRAINT fk_scheduled_jobs_rule_id_automation_rules FOREIGN KEY(rule_id) REFERENCES automation_rules (id) ON DELETE SET NULL,
	CONSTRAINT fk_scheduled_jobs_created_by_employee_id_employees FOREIGN KEY(created_by_employee_id) REFERENCES employees (id) ON DELETE SET NULL
);

CREATE TABLE tracking_clicks (
	id SERIAL NOT NULL,
	tracking_link_id INTEGER NOT NULL,
	clicked_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	referer TEXT,
	user_agent TEXT,
	ip VARCHAR,
	CONSTRAINT pk_tracking_clicks PRIMARY KEY (id),
	CONSTRAINT fk_tracking_clicks_tracking_link_id_tracking_links FOREIGN KEY(tracking_link_id) REFERENCES tracking_links (id) ON DELETE CASCADE
);

CREATE TABLE content_offers (
	id SERIAL NOT NULL,
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	item_id INTEGER NOT NULL,
	script_id INTEGER,
	mode VARCHAR NOT NULL,
	price_cents INTEGER NOT NULL,
	tip_unlock_cents INTEGER NOT NULL,
	offer_message_id BIGINT,
	status VARCHAR NOT NULL,
	resolved_by VARCHAR,
	tips_accum_cents INTEGER NOT NULL,
	delivery_message_id BIGINT,
	offered_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	resolved_at TIMESTAMP WITHOUT TIME ZONE,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_content_offers PRIMARY KEY (id),
	CONSTRAINT fk_content_offers_account_id_accounts FOREIGN KEY(account_id) REFERENCES accounts (id) ON DELETE CASCADE,
	CONSTRAINT fk_content_offers_item_id_catalog_items FOREIGN KEY(item_id) REFERENCES catalog_items (id) ON DELETE CASCADE
);

CREATE TABLE message_media (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	message_id BIGINT NOT NULL,
	media_id BIGINT NOT NULL,
	type VARCHAR,
	width INTEGER,
	height INTEGER,
	thumb_url TEXT,
	source_url TEXT,
	duration_ms INTEGER,
	ingested_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_message_media PRIMARY KEY (account_id, fan_id, message_id, media_id),
	CONSTRAINT fk_message_media_account_id_messages FOREIGN KEY(account_id, fan_id, message_id) REFERENCES messages (account_id, fan_id, message_id) ON DELETE CASCADE
);

CREATE TABLE of_message_delivery (
	account_id VARCHAR NOT NULL,
	fan_id BIGINT NOT NULL,
	message_id BIGINT NOT NULL,
	outcome VARCHAR NOT NULL,
	attempt_id VARCHAR,
	evidence_ref VARCHAR,
	detail TEXT,
	recorded_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_of_message_delivery PRIMARY KEY (account_id, fan_id, message_id),
	CONSTRAINT fk_of_message_delivery_account_id_messages FOREIGN KEY(account_id, fan_id, message_id) REFERENCES messages (account_id, fan_id, message_id) ON DELETE CASCADE
);

CREATE INDEX ix_app_errors_occurred ON app_errors (occurred_at);
CREATE INDEX ix_app_errors_source_kind ON app_errors (source, kind);
CREATE INDEX ix_audience_enroll_status ON audience_enrollments (account_id, status);
CREATE INDEX ix_runs_account_kind_started ON automation_runs (account_id, kind, started_at);
CREATE INDEX ix_runs_account_started ON automation_runs (account_id, started_at);
CREATE INDEX ix_runs_kind_started ON automation_runs (kind, started_at);
CREATE INDEX ix_employees_chatter_id ON employees (chatter_id);
CREATE INDEX ix_employees_user_id ON employees (user_id);
CREATE UNIQUE INDEX uq_employees_chatter_owner ON employees (chatter_id, user_id) WHERE chatter_id IS NOT NULL;
CREATE INDEX ix_fan_lease_expiry ON fan_lease (leased_until);
CREATE INDEX ix_nudge_state_account ON nudge_state (account_id, last_seen_online_at);
CREATE INDEX ix_perf_events_op ON perf_events (op_id);
CREATE INDEX ix_perf_events_received_at ON perf_events (received_at);
CREATE INDEX ix_perf_events_tab_kind ON perf_events (tab_id, kind);
CREATE INDEX ix_quota_audit_day ON quota_audit (account_id, day, reason);
CREATE INDEX ix_tip_reward_log_fan ON tip_reward_log (account_id, fan_id, created_at);
CREATE INDEX ix_translation_cache_created ON translation_cache (created_at);
CREATE INDEX ix_vault_sends_fan ON vault_sends (account_id, fan_id, sent_at);
CREATE INDEX ix_vault_sends_media ON vault_sends (account_id, media_id, sent_at);
CREATE INDEX ix_actions_account_at ON actions (account_id, at);
CREATE INDEX ix_actions_employee_at ON actions (employee_id, at);
CREATE UNIQUE INDEX ix_catalog_scripts_account ON catalog_scripts (account_id, name);
CREATE INDEX ix_chats_last_msg ON chats (last_message_at);
CREATE INDEX ix_chatter_account_access_chatter ON chatter_account_access (chatter_id);
CREATE INDEX ix_chatter_folder_access_chatter ON chatter_folder_access (chatter_id);
CREATE INDEX ix_chatters_invites_user_id ON chatters_invites (user_id);
CREATE INDEX ix_event_unprocessed ON event_inbox (processed_at) WHERE processed_at IS NULL;
CREATE UNIQUE INDEX uq_event_dedupe ON event_inbox (source, provider_event_id) WHERE provider_event_id IS NOT NULL;
CREATE INDEX ix_fans_last_msg ON fans (account_id, last_message_received_at);
CREATE INDEX ix_fans_paused_until ON fans (account_id, automation_paused_until) WHERE automation_paused_until IS NOT NULL;
CREATE INDEX ix_fans_relationship_stage ON fans (account_id, relationship_stage);
CREATE INDEX ix_fans_spend ON fans (account_id, lifetime_spend_cents);
CREATE INDEX ix_ladder_quote_fan ON ladder_quote (account_id, fan_id);
CREATE INDEX ix_ladder_quote_media ON ladder_quote (account_id, fan_id, media_key);
CREATE INDEX ix_ladder_state_status ON ladder_state (account_id, status);
CREATE INDEX ix_mass_broadcast_cache_account_sent_at ON mass_broadcast_cache (account_id, sent_at);
CREATE INDEX ix_mass_runs_account_sent ON mass_runs (account_id, sent_at);
CREATE INDEX ix_mass_runs_queue ON mass_runs (account_id, queue_id) WHERE queue_id IS NOT NULL;
CREATE INDEX ix_of_creator_bindings_tenant ON of_creator_bindings (tenant_id, creator_id);
CREATE UNIQUE INDEX uq_of_creator_bindings_active_account ON of_creator_bindings (account_id) WHERE revoked_at IS NULL;
CREATE INDEX ix_posts_account_status ON posts (account_id, status, scheduled_for);
CREATE INDEX ix_promo_campaigns_account ON promo_campaigns (account_id, of_promo_id);
CREATE INDEX ix_prompts_active ON prompts (name, account_id, version) WHERE is_active = TRUE;
CREATE INDEX ix_resolution_fan ON resolution_log (account_id, fan_id, status);
CREATE INDEX ix_rhythm_state_wake_at ON rhythm_state (wake_at);
CREATE INDEX ix_saved_replies_account ON saved_replies (account_id, updated_at);
CREATE INDEX ix_sessions_account_latest ON sessions (account_id, is_latest);
CREATE INDEX ix_smart_lists_account ON smart_lists (account_id);
CREATE INDEX ix_tracking_links_account ON tracking_links (account_id);
CREATE INDEX ix_tx_account_time ON transactions (account_id, occurred_at);
CREATE INDEX ix_tx_fan_time ON transactions (account_id, fan_id, occurred_at) WHERE fan_id IS NOT NULL;
CREATE INDEX ix_tx_kind ON transactions (account_id, kind, occurred_at);
CREATE INDEX ix_tx_status_occurred ON transactions (status, occurred_at);
CREATE UNIQUE INDEX uq_tx_msg ON transactions (account_id, fan_id, message_id) WHERE message_id IS NOT NULL;
CREATE UNIQUE INDEX uq_tx_provider_id ON transactions (account_id, provider_transaction_id) WHERE provider_transaction_id IS NOT NULL;
CREATE INDEX ix_trial_mirror_account ON trial_link_mirrors (account_id, of_trial_id);
CREATE INDEX ix_vault_ai_review_account_kind ON vault_ai_review_items (account_id, kind, status);
CREATE INDEX ix_vault_ai_review_account_status ON vault_ai_review_items (account_id, status);
CREATE INDEX ix_vault_cache_runs_account ON vault_cache_runs (account_id, started_at);
CREATE INDEX ix_vault_daily_usage_account_day ON vault_daily_usage (account_id, sent_on);
CREATE INDEX ix_vault_folder_items_folder ON vault_folder_items (account_id, folder_id);
CREATE INDEX ix_vault_folders_account ON vault_folders (account_id, deleted_at);
CREATE INDEX ix_vault_account_created ON vault_items (account_id, created_at);
CREATE INDEX ix_vault_account_describe ON vault_items (account_id, describe_status);
CREATE INDEX ix_vault_account_script ON vault_items (account_id, script_id, script_seq);
CREATE INDEX ix_vault_account_seen ON vault_items (account_id, last_seen_run_id);
CREATE INDEX ix_vault_account_send ON vault_items (account_id, send_count);
CREATE INDEX ix_wall_media_account_id ON wall_media (account_id);
CREATE INDEX ix_catalog_items_script ON catalog_items (account_id, script_id, position);
CREATE INDEX ix_fan_safety_flags_fan ON fan_safety_flags (account_id, fan_id);
CREATE INDEX ix_fan_safety_flags_open_critical ON fan_safety_flags (account_id, fan_id) WHERE resolved_at IS NULL AND severity = 'critical';
CREATE INDEX ix_fan_triggers_fan ON fan_triggers (account_id, fan_id);
CREATE INDEX ix_grok_fan ON grok_calls (account_id, fan_id, called_at) WHERE fan_id IS NOT NULL;
CREATE INDEX ix_grok_prompt ON grok_calls (prompt_id, called_at);
CREATE INDEX ix_grok_purpose_time ON grok_calls (purpose, called_at);
CREATE INDEX ix_messages_account_fan_time ON messages (account_id, fan_id, created_at);
CREATE INDEX ix_messages_automation_kind ON messages (account_id, automation_kind, created_at) WHERE automation_kind IS NOT NULL;
CREATE INDEX ix_messages_dir_created ON messages (account_id, direction, created_at);
CREATE INDEX ix_messages_mass ON messages (mass_run_id) WHERE mass_run_id IS NOT NULL;
CREATE INDEX ix_messages_temp ON messages (account_id, temp_id) WHERE temp_id IS NOT NULL;
CREATE INDEX ix_of_conversation_state_updated ON of_conversation_state (updated_at);
CREATE INDEX ix_jobs_account_kind_status_run ON scheduled_jobs (account_id, kind, status, run_at);
CREATE INDEX ix_jobs_due ON scheduled_jobs (run_at, status);
CREATE INDEX ix_jobs_rule_created ON scheduled_jobs (rule_id, created_at);
CREATE INDEX ix_jobs_status_run ON scheduled_jobs (status, run_at);
CREATE INDEX ix_tracking_clicks_link_time ON tracking_clicks (tracking_link_id, clicked_at);
CREATE INDEX ix_content_offers_item ON content_offers (account_id, item_id, offered_at);
CREATE INDEX ix_content_offers_open ON content_offers (account_id, fan_id, status);
CREATE INDEX ix_message_media_media ON message_media (account_id, media_id);
CREATE INDEX ix_of_message_delivery_attempt ON of_message_delivery (attempt_id);

CREATE VIEW per_employee_revenue_with_attribution AS
WITH message_linked AS (
  SELECT
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
    m.automation_kind,
    m.account_id,
    t.kind,
    t.amount_cents,
    t.occurred_at
  FROM transactions t
  INNER JOIN messages m
    ON m.account_id = t.account_id
   AND m.fan_id = t.fan_id
   AND m.message_id = t.message_id
  WHERE t.message_id IS NOT NULL
    AND t.kind IN (
      'ppv', 'ppv_message', 'ppv_post', 'ppv_stream',
      'tip', 'tip_post', 'tip_stream',
      'custom'
    )
    AND t.status IN ('cleared', 'pending')
),
standalone_tips_ranked AS (
  SELECT
    t.id AS tx_id,
    t.account_id,
    t.amount_cents,
    t.kind,
    t.occurred_at,
    COALESCE(t.attributed_employee_id, m.sent_by_employee_id) AS sent_by_employee_id,
    m.automation_kind,
    ROW_NUMBER() OVER (
      PARTITION BY t.id
      ORDER BY m.created_at DESC
    ) AS rn
  FROM transactions t
  LEFT JOIN messages m
    ON m.account_id = t.account_id
   AND m.fan_id = t.fan_id
   AND m.direction = 'out'
   AND m.sent_by_employee_id IS NOT NULL
   AND m.created_at <= t.occurred_at
   AND m.created_at > (t.occurred_at - INTERVAL '7 days')
  WHERE t.kind IN ('tip', 'tip_post', 'tip_stream')
    AND t.message_id IS NULL
    AND t.status IN ('cleared', 'pending')
),
orphan_rows AS (
  SELECT
    t.attributed_employee_id AS sent_by_employee_id,
    NULL AS automation_kind,
    t.account_id,
    t.kind,
    t.amount_cents,
    t.occurred_at
  FROM transactions t
  WHERE t.kind IN (
      'ppv', 'ppv_message', 'ppv_post', 'ppv_stream',
      'tip', 'tip_post', 'tip_stream',
      'custom'
    )
    AND t.status IN ('cleared', 'pending')
    AND NOT (t.kind IN ('tip', 'tip_post', 'tip_stream') AND t.message_id IS NULL)
    AND NOT EXISTS (
      SELECT 1 FROM messages m
       WHERE m.account_id = t.account_id
         AND m.fan_id = t.fan_id
         AND m.message_id = t.message_id
    )
)
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM message_linked
UNION ALL
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM standalone_tips_ranked
 WHERE rn = 1
UNION ALL
SELECT sent_by_employee_id, automation_kind, account_id, kind, amount_cents, occurred_at
  FROM orphan_rows;

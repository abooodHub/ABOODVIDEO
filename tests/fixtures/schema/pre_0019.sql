--
-- PostgreSQL database dump
--

-- Dumped from database version 16.4
-- Dumped by pg_dump version 16.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: transcode_job_status_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.transcode_job_status_enum AS ENUM (
    'QUEUED',
    'PROBING',
    'PROCESSING',
    'VERIFYING',
    'COMPLETED',
    'FAILED',
    'CANCELLED'
);


--
-- Name: upload_session_status_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.upload_session_status_enum AS ENUM (
    'INITIATED',
    'UPLOADING',
    'PAUSED',
    'COMPLETED',
    'CANCELLED',
    'EXPIRED'
);


--
-- Name: user_role_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.user_role_enum AS ENUM (
    'OWNER',
    'ADMIN',
    'VIP',
    'USER',
    'BANNED'
);


--
-- Name: video_status_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.video_status_enum AS ENUM (
    'UPLOADING',
    'UPLOADED',
    'QUEUED',
    'PROBING',
    'PROCESSING',
    'VERIFYING',
    'READY',
    'FAILED',
    'CANCELLED'
);


--
-- Name: video_visibility_enum; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.video_visibility_enum AS ENUM (
    'PRIVATE',
    'UNLISTED',
    'PUBLIC',
    'PASSWORD_PROTECTED'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_keys (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(100) NOT NULL,
    prefix character varying(12) NOT NULL,
    key_hash character varying(64) NOT NULL,
    scopes jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone,
    last_used_ip_hash character varying(64),
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone
);


--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_logs (
    id uuid NOT NULL,
    user_id uuid,
    action character varying(100) NOT NULL,
    target_entity character varying(100),
    target_id character varying(100),
    ip_address character varying(45),
    metadata_json jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    request_id character varying(64)
);


--
-- Name: folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.folders (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    parent_id uuid,
    name character varying(255) NOT NULL,
    folder_type character varying(32) DEFAULT 'folder'::character varying NOT NULL,
    allow_download boolean DEFAULT true NOT NULL,
    share_id character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    allow_download_policy character varying(16) DEFAULT 'ALLOW'::character varying NOT NULL,
    trashed_at timestamp with time zone
);


--
-- Name: package_files; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.package_files (
    id uuid NOT NULL,
    package_session_id uuid NOT NULL,
    relative_path character varying(255) NOT NULL,
    expected_size bigint NOT NULL,
    expected_sha256 character varying(64) NOT NULL,
    media_type character varying(50) NOT NULL,
    status character varying(20) DEFAULT 'PENDING'::character varying NOT NULL,
    received_bytes bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    verified_at timestamp with time zone
);


--
-- Name: package_upload_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.package_upload_sessions (
    id uuid NOT NULL,
    video_id uuid NOT NULL,
    created_by_key_id uuid,
    status character varying(30) DEFAULT 'INITIALIZED'::character varying NOT NULL,
    total_files integer NOT NULL,
    total_bytes bigint NOT NULL,
    completed_bytes bigint DEFAULT 0 NOT NULL,
    manifest_data jsonb NOT NULL,
    staging_path character varying(512) NOT NULL,
    target_generation_id uuid NOT NULL,
    error_message text,
    verification_job_id character varying(128),
    verification_enqueued_at timestamp with time zone,
    verification_started_at timestamp with time zone,
    finalize_attempts integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    idempotency_key character varying(64),
    request_fingerprint character varying(64)
);


--
-- Name: playback_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.playback_sessions (
    id uuid NOT NULL,
    video_id uuid NOT NULL,
    session_id character varying(64) NOT NULL,
    ip_hash character varying(64) NOT NULL,
    user_agent_short character varying(128),
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    last_heartbeat_at timestamp with time zone DEFAULT now() NOT NULL,
    duration_seconds double precision DEFAULT '0'::double precision NOT NULL,
    bytes_served bigint DEFAULT '0'::bigint NOT NULL,
    is_completed boolean DEFAULT false NOT NULL,
    quality character varying(32),
    device_type character varying(32),
    referrer_domain character varying(255),
    country_code character varying(2)
);


--
-- Name: seasons; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.seasons (
    id uuid NOT NULL,
    series_id uuid NOT NULL,
    season_number integer NOT NULL,
    display_name character varying(255) NOT NULL,
    sort_order integer DEFAULT 0 NOT NULL,
    allow_download_policy character varying(16) DEFAULT 'INHERIT'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    trashed_at timestamp with time zone
);


--
-- Name: series; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.series (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    parent_folder_id uuid,
    title character varying(255) NOT NULL,
    original_title character varying(255),
    description text,
    year integer,
    poster_path character varying(500),
    backdrop_path character varying(500),
    allow_download_policy character varying(16) DEFAULT 'INHERIT'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    trashed_at timestamp with time zone
);


--
-- Name: shares; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.shares (
    id uuid NOT NULL,
    owner_id uuid NOT NULL,
    target_type character varying(16) NOT NULL,
    target_id uuid NOT NULL,
    token character varying(64) NOT NULL,
    permission character varying(32) DEFAULT 'VIEW_ONLY'::character varying NOT NULL,
    allow_download boolean DEFAULT false NOT NULL,
    password_hash character varying(255),
    expires_at timestamp with time zone,
    revoked_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: system_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_settings (
    key character varying(100) NOT NULL,
    value text NOT NULL,
    description text,
    is_encrypted boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: transcode_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transcode_jobs (
    id uuid NOT NULL,
    video_id uuid NOT NULL,
    status public.transcode_job_status_enum DEFAULT 'QUEUED'::public.transcode_job_status_enum NOT NULL,
    priority integer DEFAULT 0 NOT NULL,
    attempt integer DEFAULT 1 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    worker_id character varying(100),
    current_rendition character varying(16),
    progress_percent double precision DEFAULT '0'::double precision NOT NULL,
    ffmpeg_fps double precision,
    ffmpeg_speed double precision,
    eta_seconds integer,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    cancel_requested boolean DEFAULT false NOT NULL,
    error_code character varying(50),
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    configuration_snapshot json
);


--
-- Name: upload_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.upload_sessions (
    id uuid NOT NULL,
    video_id uuid NOT NULL,
    user_id uuid NOT NULL,
    status public.upload_session_status_enum DEFAULT 'INITIATED'::public.upload_session_status_enum NOT NULL,
    filename character varying(255) NOT NULL,
    file_size_bytes bigint NOT NULL,
    chunk_size_bytes integer NOT NULL,
    total_chunks integer NOT NULL,
    received_chunks jsonb DEFAULT '[]'::jsonb NOT NULL,
    bytes_received bigint DEFAULT '0'::bigint NOT NULL,
    expected_sha256 character varying(64),
    final_sha256 character varying(64),
    mime_type character varying(100),
    temp_directory character varying(500) NOT NULL,
    target_path character varying(500),
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    ip_address character varying(45),
    user_agent character varying(500),
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    email character varying(255) NOT NULL,
    username character varying(64) NOT NULL,
    hashed_password character varying(255) NOT NULL,
    role public.user_role_enum DEFAULT 'USER'::public.user_role_enum NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    vip_until timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    last_authenticated_at timestamp with time zone
);


--
-- Name: video_font_assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.video_font_assets (
    id uuid NOT NULL,
    video_id uuid NOT NULL,
    generation_id uuid,
    family_name character varying(100) NOT NULL,
    filename character varying(255) NOT NULL,
    relative_path character varying(500) NOT NULL,
    sha256 character varying(64) NOT NULL,
    size_bytes bigint NOT NULL,
    mime_type character varying(64) DEFAULT 'font/ttf'::character varying NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: video_renditions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.video_renditions (
    id uuid NOT NULL,
    video_id uuid NOT NULL,
    quality_label character varying(16) NOT NULL,
    resolution_width integer NOT NULL,
    resolution_height integer NOT NULL,
    is_vip_only boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    video_codec character varying(32) DEFAULT 'h264'::character varying NOT NULL,
    audio_codec character varying(32),
    video_bitrate integer,
    audio_bitrate integer,
    playlist_path character varying(500),
    size_bytes bigint DEFAULT '0'::bigint NOT NULL,
    is_ready boolean DEFAULT false NOT NULL
);


--
-- Name: video_subtitles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.video_subtitles (
    id uuid NOT NULL,
    video_id uuid NOT NULL,
    language_code character varying(16) NOT NULL,
    language_name character varying(64) NOT NULL,
    label character varying(100) NOT NULL,
    format character varying(16) NOT NULL,
    source_path character varying(500) NOT NULL,
    webvtt_path character varying(500),
    is_default boolean DEFAULT false NOT NULL,
    is_forced boolean DEFAULT false NOT NULL,
    is_playable boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    playback_renderer character varying(32) DEFAULT 'html5'::character varying NOT NULL,
    generation_id uuid,
    font_asset_ids json
);


--
-- Name: videos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.videos (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    title character varying(255) NOT NULL,
    description text,
    status public.video_status_enum DEFAULT 'UPLOADING'::public.video_status_enum NOT NULL,
    source_filename character varying(255) NOT NULL,
    source_size_bytes bigint DEFAULT '0'::bigint NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    source_duration_seconds double precision,
    source_resolution_width integer,
    source_resolution_height integer,
    source_bitrate bigint,
    source_codec character varying(64),
    source_path character varying(500),
    upload_progress_percent double precision DEFAULT '0'::double precision NOT NULL,
    visibility public.video_visibility_enum DEFAULT 'PRIVATE'::public.video_visibility_enum NOT NULL,
    share_id character varying(64),
    share_revoked boolean DEFAULT false NOT NULL,
    viewer_password_hash character varying(255),
    poster_path character varying(1024),
    view_count bigint DEFAULT 0 NOT NULL,
    bandwidth_bytes bigint DEFAULT 0 NOT NULL,
    active_generation_id uuid,
    master_playlist_path character varying(500),
    transcode_progress_percent double precision DEFAULT '0'::double precision NOT NULL,
    category character varying(100),
    series_name character varying(150),
    season_number integer,
    episode_number integer,
    folder_id uuid,
    allow_download boolean DEFAULT true NOT NULL,
    series_id uuid,
    season_id uuid,
    allow_download_policy character varying(16) DEFAULT 'INHERIT'::character varying NOT NULL,
    trashed_at timestamp with time zone
);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (id);


--
-- Name: folders folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_pkey PRIMARY KEY (id);


--
-- Name: package_files package_files_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_files
    ADD CONSTRAINT package_files_pkey PRIMARY KEY (id);


--
-- Name: package_upload_sessions package_upload_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_upload_sessions
    ADD CONSTRAINT package_upload_sessions_pkey PRIMARY KEY (id);


--
-- Name: playback_sessions playback_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.playback_sessions
    ADD CONSTRAINT playback_sessions_pkey PRIMARY KEY (id);


--
-- Name: seasons seasons_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seasons
    ADD CONSTRAINT seasons_pkey PRIMARY KEY (id);


--
-- Name: series series_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT series_pkey PRIMARY KEY (id);


--
-- Name: shares shares_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shares
    ADD CONSTRAINT shares_pkey PRIMARY KEY (id);


--
-- Name: system_settings system_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (key);


--
-- Name: transcode_jobs transcode_jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcode_jobs
    ADD CONSTRAINT transcode_jobs_pkey PRIMARY KEY (id);


--
-- Name: upload_sessions upload_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_pkey PRIMARY KEY (id);


--
-- Name: package_files uq_package_files_session_path; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_files
    ADD CONSTRAINT uq_package_files_session_path UNIQUE (package_session_id, relative_path);


--
-- Name: package_upload_sessions uq_package_session_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_upload_sessions
    ADD CONSTRAINT uq_package_session_idempotency UNIQUE (created_by_key_id, idempotency_key);


--
-- Name: video_renditions uq_video_renditions_video_id_quality_label; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_renditions
    ADD CONSTRAINT uq_video_renditions_video_id_quality_label UNIQUE (video_id, quality_label);


--
-- Name: user_sessions user_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_pkey PRIMARY KEY (id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: video_font_assets video_font_assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_font_assets
    ADD CONSTRAINT video_font_assets_pkey PRIMARY KEY (id);


--
-- Name: video_renditions video_renditions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_renditions
    ADD CONSTRAINT video_renditions_pkey PRIMARY KEY (id);


--
-- Name: video_subtitles video_subtitles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_subtitles
    ADD CONSTRAINT video_subtitles_pkey PRIMARY KEY (id);


--
-- Name: videos videos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.videos
    ADD CONSTRAINT videos_pkey PRIMARY KEY (id);


--
-- Name: ix_api_keys_key_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_api_keys_key_hash ON public.api_keys USING btree (key_hash);


--
-- Name: ix_api_keys_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_api_keys_user_id ON public.api_keys USING btree (user_id);


--
-- Name: ix_audit_logs_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_action ON public.audit_logs USING btree (action);


--
-- Name: ix_audit_logs_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_created_at ON public.audit_logs USING btree (created_at);


--
-- Name: ix_audit_logs_request_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_request_id ON public.audit_logs USING btree (request_id);


--
-- Name: ix_audit_logs_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_logs_user_id ON public.audit_logs USING btree (user_id);


--
-- Name: ix_folders_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folders_name ON public.folders USING btree (name);


--
-- Name: ix_folders_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folders_parent_id ON public.folders USING btree (parent_id);


--
-- Name: ix_folders_share_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_folders_share_id ON public.folders USING btree (share_id);


--
-- Name: ix_folders_trashed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folders_trashed_at ON public.folders USING btree (trashed_at);


--
-- Name: ix_folders_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_folders_user_id ON public.folders USING btree (user_id);


--
-- Name: ix_package_files_package_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_package_files_package_session_id ON public.package_files USING btree (package_session_id);


--
-- Name: ix_package_files_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_package_files_status ON public.package_files USING btree (status);


--
-- Name: ix_package_upload_sessions_created_by_key_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_package_upload_sessions_created_by_key_id ON public.package_upload_sessions USING btree (created_by_key_id);


--
-- Name: ix_package_upload_sessions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_package_upload_sessions_status ON public.package_upload_sessions USING btree (status);


--
-- Name: ix_package_upload_sessions_verification_job_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_package_upload_sessions_verification_job_id ON public.package_upload_sessions USING btree (verification_job_id);


--
-- Name: ix_package_upload_sessions_video_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_package_upload_sessions_video_id ON public.package_upload_sessions USING btree (video_id);


--
-- Name: ix_playback_sessions_country_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_playback_sessions_country_code ON public.playback_sessions USING btree (country_code);


--
-- Name: ix_playback_sessions_quality; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_playback_sessions_quality ON public.playback_sessions USING btree (quality);


--
-- Name: ix_playback_sessions_referrer_domain; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_playback_sessions_referrer_domain ON public.playback_sessions USING btree (referrer_domain);


--
-- Name: ix_playback_sessions_session_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_playback_sessions_session_id ON public.playback_sessions USING btree (session_id);


--
-- Name: ix_playback_sessions_video_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_playback_sessions_video_id ON public.playback_sessions USING btree (video_id);


--
-- Name: ix_playback_sessions_video_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_playback_sessions_video_started ON public.playback_sessions USING btree (video_id, started_at);


--
-- Name: ix_seasons_season_number; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_seasons_season_number ON public.seasons USING btree (season_number);


--
-- Name: ix_seasons_series_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_seasons_series_id ON public.seasons USING btree (series_id);


--
-- Name: ix_seasons_trashed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_seasons_trashed_at ON public.seasons USING btree (trashed_at);


--
-- Name: ix_series_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_owner_id ON public.series USING btree (owner_id);


--
-- Name: ix_series_parent_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_parent_folder_id ON public.series USING btree (parent_folder_id);


--
-- Name: ix_series_title; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_title ON public.series USING btree (title);


--
-- Name: ix_series_trashed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_series_trashed_at ON public.series USING btree (trashed_at);


--
-- Name: ix_shares_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_shares_owner_id ON public.shares USING btree (owner_id);


--
-- Name: ix_shares_target_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_shares_target_id ON public.shares USING btree (target_id);


--
-- Name: ix_shares_target_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_shares_target_type ON public.shares USING btree (target_type);


--
-- Name: ix_shares_token; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_shares_token ON public.shares USING btree (token);


--
-- Name: ix_transcode_jobs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transcode_jobs_status ON public.transcode_jobs USING btree (status);


--
-- Name: ix_transcode_jobs_video_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_transcode_jobs_video_id ON public.transcode_jobs USING btree (video_id);


--
-- Name: ix_upload_sessions_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upload_sessions_expires_at ON public.upload_sessions USING btree (expires_at);


--
-- Name: ix_upload_sessions_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upload_sessions_status ON public.upload_sessions USING btree (status);


--
-- Name: ix_upload_sessions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upload_sessions_user_id ON public.upload_sessions USING btree (user_id);


--
-- Name: ix_upload_sessions_video_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_upload_sessions_video_id ON public.upload_sessions USING btree (video_id);


--
-- Name: ix_user_sessions_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_sessions_expires_at ON public.user_sessions USING btree (expires_at);


--
-- Name: ix_user_sessions_token_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_user_sessions_token_hash ON public.user_sessions USING btree (token_hash);


--
-- Name: ix_user_sessions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_user_sessions_user_id ON public.user_sessions USING btree (user_id);


--
-- Name: ix_users_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_email ON public.users USING btree (email);


--
-- Name: ix_users_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_users_role ON public.users USING btree (role);


--
-- Name: ix_users_username; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_users_username ON public.users USING btree (username);


--
-- Name: ix_video_font_assets_family_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_font_assets_family_name ON public.video_font_assets USING btree (family_name);


--
-- Name: ix_video_font_assets_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_font_assets_generation_id ON public.video_font_assets USING btree (generation_id);


--
-- Name: ix_video_font_assets_video_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_font_assets_video_id ON public.video_font_assets USING btree (video_id);


--
-- Name: ix_video_renditions_video_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_renditions_video_id ON public.video_renditions USING btree (video_id);


--
-- Name: ix_video_subtitles_generation_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_subtitles_generation_id ON public.video_subtitles USING btree (generation_id);


--
-- Name: ix_video_subtitles_language_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_subtitles_language_code ON public.video_subtitles USING btree (language_code);


--
-- Name: ix_video_subtitles_video_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_subtitles_video_id ON public.video_subtitles USING btree (video_id);


--
-- Name: ix_video_subtitles_video_lang; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_video_subtitles_video_lang ON public.video_subtitles USING btree (video_id, language_code);


--
-- Name: ix_videos_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_category ON public.videos USING btree (category);


--
-- Name: ix_videos_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_folder_id ON public.videos USING btree (folder_id);


--
-- Name: ix_videos_season_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_season_id ON public.videos USING btree (season_id);


--
-- Name: ix_videos_series_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_series_id ON public.videos USING btree (series_id);


--
-- Name: ix_videos_series_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_series_name ON public.videos USING btree (series_name);


--
-- Name: ix_videos_share_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_videos_share_id ON public.videos USING btree (share_id);


--
-- Name: ix_videos_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_status ON public.videos USING btree (status);


--
-- Name: ix_videos_title; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_title ON public.videos USING btree (title);


--
-- Name: ix_videos_trashed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_trashed_at ON public.videos USING btree (trashed_at);


--
-- Name: ix_videos_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_user_id ON public.videos USING btree (user_id);


--
-- Name: ix_videos_visibility; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_videos_visibility ON public.videos USING btree (visibility);


--
-- Name: uq_transcode_jobs_active_video; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_transcode_jobs_active_video ON public.transcode_jobs USING btree (video_id) WHERE (status = ANY (ARRAY['QUEUED'::public.transcode_job_status_enum, 'PROBING'::public.transcode_job_status_enum, 'PROCESSING'::public.transcode_job_status_enum, 'VERIFYING'::public.transcode_job_status_enum]));


--
-- Name: uq_video_subtitles_one_default_per_video; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_video_subtitles_one_default_per_video ON public.video_subtitles USING btree (video_id) WHERE (is_default = true);


--
-- Name: api_keys api_keys_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: audit_logs audit_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: folders folders_parent_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES public.folders(id) ON DELETE CASCADE;


--
-- Name: folders folders_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: package_files package_files_package_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_files
    ADD CONSTRAINT package_files_package_session_id_fkey FOREIGN KEY (package_session_id) REFERENCES public.package_upload_sessions(id) ON DELETE CASCADE;


--
-- Name: package_upload_sessions package_upload_sessions_created_by_key_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_upload_sessions
    ADD CONSTRAINT package_upload_sessions_created_by_key_id_fkey FOREIGN KEY (created_by_key_id) REFERENCES public.api_keys(id) ON DELETE SET NULL;


--
-- Name: package_upload_sessions package_upload_sessions_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.package_upload_sessions
    ADD CONSTRAINT package_upload_sessions_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;


--
-- Name: playback_sessions playback_sessions_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.playback_sessions
    ADD CONSTRAINT playback_sessions_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;


--
-- Name: seasons seasons_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.seasons
    ADD CONSTRAINT seasons_series_id_fkey FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE CASCADE;


--
-- Name: series series_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT series_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: series series_parent_folder_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.series
    ADD CONSTRAINT series_parent_folder_id_fkey FOREIGN KEY (parent_folder_id) REFERENCES public.folders(id) ON DELETE SET NULL;


--
-- Name: shares shares_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.shares
    ADD CONSTRAINT shares_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: transcode_jobs transcode_jobs_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transcode_jobs
    ADD CONSTRAINT transcode_jobs_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;


--
-- Name: upload_sessions upload_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: upload_sessions upload_sessions_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;


--
-- Name: user_sessions user_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_sessions
    ADD CONSTRAINT user_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: video_font_assets video_font_assets_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_font_assets
    ADD CONSTRAINT video_font_assets_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;


--
-- Name: video_renditions video_renditions_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_renditions
    ADD CONSTRAINT video_renditions_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;


--
-- Name: video_subtitles video_subtitles_video_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.video_subtitles
    ADD CONSTRAINT video_subtitles_video_id_fkey FOREIGN KEY (video_id) REFERENCES public.videos(id) ON DELETE CASCADE;


--
-- Name: videos videos_folder_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.videos
    ADD CONSTRAINT videos_folder_id_fkey FOREIGN KEY (folder_id) REFERENCES public.folders(id) ON DELETE SET NULL;


--
-- Name: videos videos_season_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.videos
    ADD CONSTRAINT videos_season_id_fkey FOREIGN KEY (season_id) REFERENCES public.seasons(id) ON DELETE SET NULL;


--
-- Name: videos videos_series_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.videos
    ADD CONSTRAINT videos_series_id_fkey FOREIGN KEY (series_id) REFERENCES public.series(id) ON DELETE SET NULL;


--
-- Name: videos videos_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.videos
    ADD CONSTRAINT videos_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


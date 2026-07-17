\restrict PzmxiYfzaXh8M3ZQECL5wxOX0stRFDiczX3KJhTI9RSoVOt8ZjH2cOGc6ug5oTX

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg13+1)
-- Dumped by pg_dump version 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1)

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

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.batches (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    photo_count integer DEFAULT 0 NOT NULL,
    origin_url text,
    notify_on_complete boolean DEFAULT false NOT NULL,
    notified_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone,
    CONSTRAINT batches_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'processing'::text, 'complete'::text, 'deferred'::text, 'failed'::text])))
);


--
-- Name: card_refs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.card_refs (
    id text NOT NULL,
    name text NOT NULL,
    set_id text NOT NULL,
    set_name text NOT NULL,
    number text NOT NULL,
    printed_total text,
    rarity text,
    image_url text NOT NULL,
    finishes text[] DEFAULT '{}'::text[] NOT NULL,
    synced_at timestamp with time zone DEFAULT now() NOT NULL,
    ref_cached_key text
);


--
-- Name: cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cards (
    id uuid NOT NULL,
    photo_id uuid NOT NULL,
    crop_index integer NOT NULL,
    crop_storage_key text,
    card_ref_id text,
    finish text,
    finish_needs_confirmation boolean DEFAULT false NOT NULL,
    quantity integer DEFAULT 1 NOT NULL,
    confidence integer DEFAULT 0 NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    accepted_stage text,
    rotation integer DEFAULT 0 NOT NULL,
    method_h_id text,
    method_h_score real,
    method_a_id text,
    method_a_score real,
    method_b_id text,
    method_b_score real,
    method_c_id text,
    method_c_score real,
    candidates jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT cards_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'auto'::text, 'validation'::text, 'validated'::text, 'corrected'::text, 'skipped'::text, 'not_card'::text, 'unreadable'::text, 'merged'::text])))
);


--
-- Name: corrections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.corrections (
    id uuid NOT NULL,
    card_id uuid,
    crop_hash text NOT NULL,
    predicted_ref_id text,
    actual_ref_id text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id uuid NOT NULL,
    type text NOT NULL,
    payload jsonb NOT NULL,
    status text DEFAULT 'queued'::text NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 3 NOT NULL,
    run_after timestamp with time zone DEFAULT now() NOT NULL,
    locked_at timestamp with time zone,
    locked_by text,
    last_error text,
    batch_id uuid,
    user_id uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT jobs_status_check CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'done'::text, 'failed'::text]))),
    CONSTRAINT jobs_type_check CHECK ((type = ANY (ARRAY['detect'::text, 'identify'::text, 'fetch_source'::text, 'ingest_correction'::text, 'price'::text])))
);


--
-- Name: llm_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_cache (
    crop_sha256 text NOT NULL,
    model text NOT NULL,
    response jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: magic_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.magic_links (
    id uuid NOT NULL,
    email text NOT NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone
);


--
-- Name: photos; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.photos (
    id uuid NOT NULL,
    batch_id uuid NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    storage_key text,
    source_type text DEFAULT 'upload'::text NOT NULL,
    source_url text,
    bytes bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT photos_source_type_check CHECK ((source_type = ANY (ARRAY['upload'::text, 'imgur'::text, 'reddit'::text]))),
    CONSTRAINT photos_status_check CHECK ((status = ANY (ARRAY['pending'::text, 'fetching'::text, 'stored'::text, 'detecting'::text, 'done'::text, 'failed'::text])))
);


--
-- Name: prices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.prices (
    card_ref_id text NOT NULL,
    finish text NOT NULL,
    price_cents integer,
    source text NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: ref_hashes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ref_hashes (
    id uuid NOT NULL,
    card_ref_id text NOT NULL,
    hash_type text NOT NULL,
    hash_bits bigint NOT NULL,
    source text NOT NULL,
    usage_count integer DEFAULT 0 NOT NULL,
    last_matched_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ref_hashes_hash_type_check CHECK ((hash_type = ANY (ARRAY['full'::text, 'edge'::text, 'region_art'::text, 'region_name'::text, 'region_text'::text]))),
    CONSTRAINT ref_hashes_source_check CHECK ((source = ANY (ARRAY['reference'::text, 'augmented'::text, 'user_validated'::text])))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying(128) NOT NULL
);


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_hash text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_seen_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: usage; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage (
    user_id uuid NOT NULL,
    day date NOT NULL,
    batches integer DEFAULT 0 NOT NULL,
    photos integer DEFAULT 0 NOT NULL,
    cards integer DEFAULT 0 NOT NULL,
    llm_calls integer DEFAULT 0 NOT NULL,
    llm_cost_cents integer DEFAULT 0 NOT NULL,
    fetches integer DEFAULT 0 NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid NOT NULL,
    email text,
    anon_token_hash text,
    tier text DEFAULT 'free'::text NOT NULL,
    status text DEFAULT 'active'::text NOT NULL,
    storage_bytes_used bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT users_check CHECK (((email IS NOT NULL) OR (anon_token_hash IS NOT NULL))),
    CONSTRAINT users_status_check CHECK ((status = ANY (ARRAY['active'::text, 'suspended'::text]))),
    CONSTRAINT users_tier_check CHECK ((tier = ANY (ARRAY['anon'::text, 'free'::text])))
);


--
-- Name: batches batches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batches
    ADD CONSTRAINT batches_pkey PRIMARY KEY (id);


--
-- Name: card_refs card_refs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.card_refs
    ADD CONSTRAINT card_refs_pkey PRIMARY KEY (id);


--
-- Name: cards cards_photo_id_crop_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_photo_id_crop_index_key UNIQUE (photo_id, crop_index);


--
-- Name: cards cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_pkey PRIMARY KEY (id);


--
-- Name: corrections corrections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.corrections
    ADD CONSTRAINT corrections_pkey PRIMARY KEY (id);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: llm_cache llm_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_cache
    ADD CONSTRAINT llm_cache_pkey PRIMARY KEY (crop_sha256);


--
-- Name: magic_links magic_links_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.magic_links
    ADD CONSTRAINT magic_links_pkey PRIMARY KEY (id);


--
-- Name: magic_links magic_links_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.magic_links
    ADD CONSTRAINT magic_links_token_hash_key UNIQUE (token_hash);


--
-- Name: photos photos_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photos
    ADD CONSTRAINT photos_pkey PRIMARY KEY (id);


--
-- Name: prices prices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prices
    ADD CONSTRAINT prices_pkey PRIMARY KEY (card_ref_id, finish);


--
-- Name: ref_hashes ref_hashes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_hashes
    ADD CONSTRAINT ref_hashes_pkey PRIMARY KEY (id);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_token_hash_key UNIQUE (token_hash);


--
-- Name: usage usage_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage
    ADD CONSTRAINT usage_pkey PRIMARY KEY (user_id, day);


--
-- Name: users users_anon_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_anon_token_hash_key UNIQUE (anon_token_hash);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: batches_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX batches_user_idx ON public.batches USING btree (user_id, created_at DESC);


--
-- Name: card_refs_name_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX card_refs_name_idx ON public.card_refs USING btree (lower(name));


--
-- Name: card_refs_number_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX card_refs_number_idx ON public.card_refs USING btree (number);


--
-- Name: cards_photo_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX cards_photo_idx ON public.cards USING btree (photo_id);


--
-- Name: jobs_claim_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX jobs_claim_idx ON public.jobs USING btree (status, run_after);


--
-- Name: magic_links_email_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX magic_links_email_idx ON public.magic_links USING btree (email, created_at);


--
-- Name: photos_batch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX photos_batch_idx ON public.photos USING btree (batch_id);


--
-- Name: ref_hashes_card_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_hashes_card_idx ON public.ref_hashes USING btree (card_ref_id);


--
-- Name: ref_hashes_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_hashes_type_idx ON public.ref_hashes USING btree (hash_type);


--
-- Name: batches batches_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.batches
    ADD CONSTRAINT batches_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: cards cards_card_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_card_ref_id_fkey FOREIGN KEY (card_ref_id) REFERENCES public.card_refs(id);


--
-- Name: cards cards_photo_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cards
    ADD CONSTRAINT cards_photo_id_fkey FOREIGN KEY (photo_id) REFERENCES public.photos(id) ON DELETE CASCADE;


--
-- Name: corrections corrections_card_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.corrections
    ADD CONSTRAINT corrections_card_id_fkey FOREIGN KEY (card_id) REFERENCES public.cards(id) ON DELETE SET NULL;


--
-- Name: jobs jobs_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batches(id) ON DELETE CASCADE;


--
-- Name: jobs jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: photos photos_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.photos
    ADD CONSTRAINT photos_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.batches(id) ON DELETE CASCADE;


--
-- Name: prices prices_card_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.prices
    ADD CONSTRAINT prices_card_ref_id_fkey FOREIGN KEY (card_ref_id) REFERENCES public.card_refs(id) ON DELETE CASCADE;


--
-- Name: ref_hashes ref_hashes_card_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_hashes
    ADD CONSTRAINT ref_hashes_card_ref_id_fkey FOREIGN KEY (card_ref_id) REFERENCES public.card_refs(id) ON DELETE CASCADE;


--
-- Name: sessions sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: usage usage_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage
    ADD CONSTRAINT usage_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict PzmxiYfzaXh8M3ZQECL5wxOX0stRFDiczX3KJhTI9RSoVOt8ZjH2cOGc6ug5oTX


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('001'),
    ('002'),
    ('003');

\restrict 6smYCbbva5WsdBzb07SQcYJSSvWwfn3lxxf5XIn7hDIcr3VZQfjISSa2lOzhnXl

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
    synced_at timestamp with time zone DEFAULT now() NOT NULL
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
-- Name: card_refs card_refs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.card_refs
    ADD CONSTRAINT card_refs_pkey PRIMARY KEY (id);


--
-- Name: llm_cache llm_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_cache
    ADD CONSTRAINT llm_cache_pkey PRIMARY KEY (crop_sha256);


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
-- Name: card_refs_name_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX card_refs_name_idx ON public.card_refs USING btree (lower(name));


--
-- Name: card_refs_number_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX card_refs_number_idx ON public.card_refs USING btree (number);


--
-- Name: ref_hashes_card_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_hashes_card_idx ON public.ref_hashes USING btree (card_ref_id);


--
-- Name: ref_hashes_type_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ref_hashes_type_idx ON public.ref_hashes USING btree (hash_type);


--
-- Name: ref_hashes ref_hashes_card_ref_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ref_hashes
    ADD CONSTRAINT ref_hashes_card_ref_id_fkey FOREIGN KEY (card_ref_id) REFERENCES public.card_refs(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict 6smYCbbva5WsdBzb07SQcYJSSvWwfn3lxxf5XIn7hDIcr3VZQfjISSa2lOzhnXl


--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('001');

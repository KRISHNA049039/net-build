CREATE KEYSPACE IF NOT EXISTS ans_transformed
WITH replication = {'class': 'NetworkTopologyStrategy', 'datacenter-1': 3};

USE ans_transformed;

CREATE TABLE ff_net_report (
    ff_id int,
    frequency double,
    bandwidth double,
    modulation text,
    signal_type text,
    net_level text,
    net_type text,
    branch text,
    nationality text,
    force text,
    language text,
    cipher text,
    latitude double,
    longitude double,
    geo_distance double,
    nti int,
    fti date,
    lti date,
    azimuth double,
    elevation double,
    snr_db double,
    rdfs text,
    PRIMARY KEY ((ff_id), lti)
) WITH CLUSTERING ORDER BY (lti DESC);

/*To be removed in airgapped added for just ref not consumed by code*/

-- ============================================================================
-- Net-building output: ONE row per net (SYSTEM_NET). MY_NET groups nets in
-- the UI; today the pipeline always creates a MY_NET/SYSTEM_NET pair together,
-- so my_net_id is 1:1 with net_id, but it's stored so that can change later.
-- Fully derived from ff_net_report + ff_net_report_history; safe to rebuild.
-- ============================================================================
CREATE TABLE ff_net_building (
    net_id int,
    my_net_id int,
    ff_id int,
    lti date,
    fti date,
    frequency double,
    bandwidth double,
    modulation text,
    signal_type text,
    net_level text,
    net_type text,
    branch text,
    nationality text,
    force text,
    language text,
    cipher text,
    latitude double,
    longitude double,
    geo_distance double,
    nti int,
    azimuth double,
    elevation double,
    snr_db double,
    rdfs text,
    updated_at timestamp,
    PRIMARY KEY (net_id)
);

-- ============================================================================
-- Historic audit trail: append-only, one row per raw report ever folded into
-- a net across all pipeline runs. Never truncated/deleted.
-- ============================================================================
CREATE TABLE ff_net_report_history (
    net_id int,
    my_net_id int,
    processed_at timestamp,
    ff_id int,
    lti date,
    fti date,
    nti int,
    frequency double,
    bandwidth double,
    modulation text,
    signal_type text,
    rdfs text,
    action text,            -- 'new' | 'collapsed' (skipped reports are never logged)
    collapsed_ff_id int,
    was_collapsed boolean,
    net_level text,
    net_type text,
    branch text,
    nationality text,
    force text,
    language text,
    cipher text,
    latitude double,
    longitude double,
    geo_distance double,
    azimuth double,
    elevation double,
    snr_db double,
    PRIMARY KEY ((net_id), processed_at, ff_id)
) WITH CLUSTERING ORDER BY (processed_at DESC, ff_id ASC);

-- ============================================================================
-- Net-to-net merge candidates: a persistent ledger of every (net_a, net_b)
-- pair ever proposed by net_merging.propose_pairs, plus its human decision.
-- 'pending' rows are the commander-facing alert queue. Approving/rejecting
-- moves a row to 'approved'/'rejected' and it is never re-proposed -- this
-- table doubles as both the alert queue AND the idempotency ledger (same
-- role ff_net_report_history plays for the build pipeline). Merging itself
-- never deletes/recreates ff_net_building rows -- it only reassigns
-- my_net_id on the absorbed group's rows (see net_merging_service.
-- apply_decision). net_a < net_b always (canonical pair ordering).
-- ============================================================================
CREATE TABLE ff_net_merge_candidates (
    net_a int,
    net_b int,
    status text,             -- 'pending' | 'approved' | 'rejected'
    my_net_id_a int,         -- snapshot at detection time (for display even
    my_net_id_b int,         -- if a later merge changes the live value)
    rdfs_a text,
    rdfs_b text,
    frequency_a double,
    frequency_b double,
    lti_a date,
    lti_b date,
    freq_gap_mhz double,
    freq_tol_mhz double,
    lti_gap_sec double,
    lti_tol_sec double,
    distance_m double,
    loc_tol_m double,
    detected_at timestamp,
    decided_at timestamp,
    PRIMARY KEY (net_a, net_b)
);
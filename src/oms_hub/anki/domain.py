from enum import StrEnum


class CurationState(StrEnum):
    QUEUED = "queued"
    BUILDING_LCL = "building_lcl"
    RETRIEVING = "retrieving"
    JUDGING = "judging"
    DEDUPING = "deduping"
    PROPOSING_GAPS = "proposing_gaps"
    READY_FOR_REVIEW = "ready_for_review"
    ENVELOPE_PENDING = "envelope_pending"
    APPLYING = "applying"
    COMPLETE = "complete"
    FAILED = "failed"


class CurationStage(StrEnum):
    LCL = "lcl"
    RETRIEVAL = "retrieval"
    JUDGMENT = "judgment"
    DEDUPE = "dedupe"
    GAPS = "gaps"
    MEDIA = "media"
    ENVELOPE = "envelope"


class Verdict(StrEnum):
    INCLUDE = "include"
    UNCERTAIN = "uncertain"
    DROP = "drop"


class AgentCommandType(StrEnum):
    FULL_SNAPSHOT = "full_snapshot"
    DELTA_SNAPSHOT = "delta_snapshot"
    FETCH_MEDIA = "fetch_media"
    APPLY_ENVELOPE = "apply_envelope"


class EnvelopeOperationType(StrEnum):
    STORE_MEDIA = "store_media"
    ADD_TAGS = "add_tags"
    ADD_NOTES = "add_notes"
    SYNC = "sync"
    VERIFY = "verify"

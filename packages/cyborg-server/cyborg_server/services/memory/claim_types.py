"""Claim type registry and deterministic entity template renderer."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from jinja2 import Environment, FileSystemLoader, TemplateNotFound


@dataclass(slots=True)
class ClaimType:
    key: str
    applicable_types: list[str]
    description: str
    example: str


@dataclass
class EntityType:
    """Centralized metadata for an entity type.

    All per-type configuration that was previously scattered across
    reconciliation.py, service.py, claim_service.py, and prompts.py
    lives here. Adding a new entity type only requires adding an entry
    to ENTITY_TYPE_REGISTRY.
    """
    name: str
    prefix: str                          # "person-", "trip-", etc.
    description: str                     # for extraction prompt glossary
    keywords: list[str]                  # for text-based entity type detection
    triggers_types: list[str]            # types to include when keywords match
    extraction_rules: list[str]          # per-type rules for extraction prompt
    reconciliation_rules: str            # rules text for reconciliation prompt
    skip_expand: bool = False            # skip during recursive recon expansion
    follow_for_bulletins: bool = False   # follow entity refs for bulletin collection
    display_name_claim: str | None = None  # claim key for display name lookup
    skip_new_patterns: list[str] | None = None  # patterns to skip during entity creation
    has_orphan_linker: bool = False      # needs orphan entity discovery in reconciliation


# Hardcoded registry — must match 317_claim_types.sql + 321 migration.
CLAIM_TYPE_REGISTRY: dict[str, ClaimType] = {}

_RAW_TYPES: list[tuple[str, list[str], str, str]] = [
    # Person
    ("alias", ["person", "group", "location"], "Alternative name or nickname (e.g. 'Cleaver', 'Dave'). Not for event status, actions, or phrases", 'person-mike-cleaver → "Cleaver"'),
    ("appearance", ["person"], "Physical description", 'person-mike-cleaver → "tall, short brown hair, glasses"'),
    ("spouse", ["person"], "Spouse or partner", "person-mike-cleaver → person-blair-nicol"),
    ("parent", ["person"], "Parent of this person", "person-mike-cleaver → person-mum"),
    ("child", ["person"], "Child of this person", "person-mike-cleaver → person-bob-jnr"),
    ("sibling", ["person"], "Brother or sister", "person-mike-cleaver → person-sam-parry"),
    ("grandparent", ["person"], "Grandparent of this person", "person-mike-cleaver → person-grandpa"),
    ("grandchild", ["person"], "Grandchild of this person", "person-mike-cleaver → person-new-baby"),
    ("home_address", ["person"], "Where they live", 'person-mike-cleaver → "42 Bondi Rd, Sydney"'),
    ("workplace", ["person"], "Where they work", 'person-mike-cleaver → "Google, Sydney office"'),
    ("job", ["person"], "What they do for work", 'person-mike-cleaver → "Software Engineer"'),
    ("food_preference", ["person"], "Food likes and dislikes", 'person-mike-cleaver → "loves Thai food, hates coriander"'),
    ("drink_preference", ["person"], "Drink likes and dislikes", 'person-mike-cleaver → "prefers red wine, no beer"'),
    ("dietary_restriction", ["person"], "Dietary needs, allergies, restrictions", 'person-mike-cleaver → "celiac, shellfish allergy"'),
    ("interest", ["person"], "Hobbies, passions, activities", 'person-mike-cleaver → "surfing, photography"'),
    ("personality", ["person"], "Temperament and character traits", 'person-mike-cleaver → "easygoing, punctual"'),
    ("language", ["person"], "Languages spoken", 'person-mike-cleaver → "English, conversational Indonesian"'),
    ("birthday", ["person"], "Date of birth", 'person-mike-cleaver → "1990-03-15"'),
    ("contact_method", ["person"], "Phone number, email address, or messaging handle only (e.g. '+61 400 123 456', 'email: mike@example.com', '@handle'). Not for conversation summaries, instructions, or actions", 'person-mike-cleaver → "email: mike@example.com"'),
    ("hometown", ["person"], "Where they grew up", 'person-mike-cleaver → "Melbourne"'),
    ("contact_id", ["person"], "Links to a contacts table row (value = hex8 ID)", "person-mike-cleaver → 7c9f0fd7"),
    # Group
    ("purpose", ["group", "event", "trip", "file", "thing", "task"], "What this entity is for", 'group-bali-gang → "planning the family Bali trip"'),
    ("vibe", ["group"], "How people act in the group", 'group-bali-gang → "casual, lots of banter"'),
    ("member", ["group", "trip"], "Person who belongs to this group or trip", "group-bali-gang → person-mike-cleaver"),
    # Event
    ("name", ["event", "file", "thing", "task"], "Name or title", 'event-dinner-aug5 → "Dinner at Mama San"'),
    ("start_time", ["event"], "When it starts or departs", 'event-dinner-aug5 → "2026-08-05T19:00"'),
    ("end_time", ["event"], "When it ends", 'event-dinner-aug5 → "2026-08-05T22:00"'),
    ("location", ["event"], "Where it takes place", "event-dinner-aug5 → location-mama-san"),
    ("organizer", ["event"], "Who is running or hosting it", "event-dinner-aug5 → person-mike-cleaver"),
    ("attendee", ["event"], "Who is attending", "event-dinner-aug5 → person-david-shedden"),
    ("recurrence", ["event"], "Recurring pattern or one-off", 'event-dinner-aug5 → "one-off"'),
    ("associated_trip", ["event", "attraction", "dayplan"], "Trip this relates to", "event-dinner-aug5 → trip-bali-2026"),
    # Location
    ("location_type", ["location"], "Kind of place", 'location-villa-sunset → "villa"'),
    ("parent_location", ["location"], "Location this is contained within", "location-villa-sunset → location-seminyak"),
    ("address", ["location"], "Street address or directions", 'location-villa-sunset → "Jl. Kayu Aya No. 50"'),
    ("associated_contact", ["location"], "Person who lives there or owns it", "location-mike-house → person-mike-cleaver"),
    ("interest", ["location"], "What makes this place appealing or noteworthy", 'location-tanah-lot → "famous sea temple with sunset views"'),
    ("opening_hours", ["location"], "Opening hours (not for cities/countries)", 'location-mama-san → "12:00-23:00 daily"'),
    # Trip
    ("leg", ["trip"], "A Stay that is part of this trip", "trip-bali-2026 → stay-bali-day1-3"),
    ("attraction", ["trip"], "An Attraction entity — a thing to see or do on the trip", "trip-bali-2026 → attraction-tanah-lot"),
    ("dayplan", ["trip"], "A Dayplan entity — planned itinerary for a specific day", "trip-bali-2026 → dayplan-bali-aug3"),
    ("connection", ["trip"], "A Connection entity that is part of this trip",
     "trip-europe-france → connection-perth-geneva-outbound"),
    # Connection
    ("departure_location", ["connection"], "Where the journey starts (location or city name)",
     'connection-perth-geneva → "Perth PER T1"'),
    ("arrival_location", ["connection"], "Where the journey ends (location or city name)",
     'connection-perth-geneva → "Geneva GVA T1"'),
    ("departure_time", ["connection"], "Departure date/time",
     'connection-perth-geneva → "2026-06-22T15:50"'),
    ("arrival_time", ["connection"], "Arrival date/time",
     'connection-perth-geneva → "2026-06-23T10:50"'),
    ("transport_type", ["connection"], "Mode: flight, train, bus, ferry, car, taxi, other",
     'connection-perth-geneva → "flight"'),
    ("duration", ["connection"], "Journey duration",
     'connection-perth-geneva → "25h"'),
    ("booking_ref", ["connection"], "Booking reference, PNR, or confirmation code",
     'connection-perth-geneva → "EPBT7N"'),
    ("route", ["connection"], "Route details: flight numbers, train numbers, intermediate stops",
     'connection-perth-geneva → "MH124 PER→KUL, MH002 KUL→LHR, BA744 LHR→GVA"'),
    ("passenger", ["connection"], "Person traveling on this connection",
     "connection-perth-geneva → person-mike-cleaver"),
    ("seat", ["connection"], "Seat or cabin assignment",
     'connection-perth-geneva → "Coach 10, Seat 32"'),
    # Attraction
    ("attraction_type", ["attraction"], "Kind of attraction: temple, museum, beach, market, viewpoint, park, restaurant, bar, activity, tour",
     'attraction-tanah-lot → "temple"'),
    ("visit_date", ["attraction"], "Date/time when visiting", 'attraction-tanah-lot → "2026-08-03T16:00"'),
    ("cost", ["attraction"], "Cost or ticket price", 'attraction-tanah-lot → "50k IDR"'),
    ("location", ["attraction"], "Where the attraction is", "attraction-tanah-lot → location-tanah-lot"),
    # Dayplan
    ("date", ["dayplan"], "Date this dayplan covers", 'dayplan-bali-aug3 → "2026-08-03"'),
    ("notes", ["dayplan"], "Ideas, plans, or notes for the day", 'dayplan-bali-aug3 → "Morning at beach, afternoon temple visit"'),
    ("attraction", ["dayplan"], "Attraction planned for this day", "dayplan-bali-aug3 → attraction-tanah-lot"),
    # (associated_trip also applies to event and attraction — see Event section above)
    # Stay
    ("accommodation", ["stay"], "Location where you stay", "stay-bali-day1-3 → location-villa-sunset"),
    ("accommodation_type", ["stay"], "Type of accommodation: hotel, airbnb, hostel, camping, resort, apartment, villa", 'stay-paris-june12-14 → "hotel"'),
    ("accommodation_address", ["stay"], "Street address of the accommodation", 'stay-paris-june12-14 → "12 Rue de Rivoli, Paris 75001"'),
    ("arrival_date", ["stay"], "Date/time of arrival", 'stay-bali-day1-3 → "2026-08-01T14:00"'),
    ("departure_date", ["stay"], "Date/time of departure", 'stay-bali-day1-3 → "2026-08-03T10:00"'),
    # Task
    ("owner", ["task", "file", "thing"], "Person responsible", "task-book-villa → person-mike-cleaver"),
    ("due_date", ["task"], "Deadline", 'task-book-villa → "2026-07-01"'),
    ("description", ["task", "thing"], "What needs doing or what this is", 'task-book-villa → "Compare 3 villa options and book"'),
    ("task_status", ["task"], "Status: open, in-progress, done, blocked", 'task-book-villa → "in-progress"'),
    ("related_entity", ["task", "decision", "file", "thing"], "Entity this belongs to", "task-book-villa → trip-bali-2026"),
    # File
    ("file_path", ["file"], "Where the file lives (workspace path or URL)", 'file-villa-spreadsheet → "https://docs.google.com/..."'),
    # Thing
    ("thing_type", ["thing"], "Kind of physical thing: animal, toy, tool, vehicle, furniture, appliance, food, device", 'thing-ebike → "vehicle"'),
    # Decision
    ("decider", ["decision"], "Who made the decision", "decision-stay-seminyak → person-mike-cleaver"),
    ("rationale", ["decision"], "Why this decision was made", 'decision-stay-seminyak → "Close to restaurants and beach"'),
    # Person
    ("preference", ["person"], "General preference or style (not food/drink — use those specific types). e.g. prefers dark mode, likes brief updates, wants to be asked before acting", 'person-mike-cleaver → "prefers concise summaries, no chitchat"'),
    # Cross-cutting
    ("file_ref", ["person", "group", "location", "trip", "stay", "event", "task", "file", "thing", "decision", "attraction", "dayplan"],
     "Links to a file entity (object_id must be a file-* entity ID). Not for notes, actions, or non-file entity references", "trip-bali-2026 → file-villa-spreadsheet"),
    ("truth", ["person", "group", "location", "trip", "stay", "event", "task", "file", "thing", "decision", "attraction", "dayplan"],
     "ONLY for explicit user corrections to existing memory ('actually...', 'no it's X', 'that's wrong'). NOT for observations, actions, requests, preferences, or general facts — use the specific claim type instead.", 'trip-mike-holiday-june-2026 → "No, we changed to 2 stops in Paris not 1"'),
]

for _key, _types, _desc, _ex in _RAW_TYPES:
    CLAIM_TYPE_REGISTRY[_key] = ClaimType(key=_key, applicable_types=_types, description=_desc, example=_ex)

del _RAW_TYPES

# ---------------------------------------------------------------------------
# Entity type registry — single source of truth for per-type metadata
# ---------------------------------------------------------------------------

ENTITY_TYPE_REGISTRY: dict[str, EntityType] = {
    "person": EntityType(
        name="person",
        prefix="person-",
        description=(
            "A specific, individual human being. Only create person entities for real people "
            "mentioned by name in the bulletin. Do NOT create persons for: bots, AI assistants, "
            "companies, teams, tools, services, concepts, places, dates, phone numbers, or software."
        ),
        keywords=[],
        triggers_types=["person"],
        extraction_rules=[
            "person entities are REAL HUMANS ONLY. Never create persons for: bots, subagents, AI models, "
            "tools, services, APIs, phone numbers, companies, places, dates, concepts, inanimate objects, "
            "software, scripts, workflows, folders, or anything that is not a specific human being. "
            "When in doubt, do NOT create a person entity.",
        ],
        reconciliation_rules=(
            "1. A person MUST NOT have a parent, child, or partner claim that references themselves.\n"
            "2. If a person has both parent and partner claims to the same entity, "
            "the parent claim is likely wrong — retract it.\n"
            "3. Semantically duplicate claims (same fact worded differently, e.g. "
            "'coeliac/GF' and 'coeliac-safe options') should be retracted in favor of "
            "the most specific/sourced version.\n"
            "4. Inferred claims with no source bulletin are less reliable than "
            "bulletin-grounded claims. If they conflict, prefer the sourced claim."
        ),
        skip_expand=True,
        follow_for_bulletins=False,
        display_name_claim="contact_id",
        skip_new_patterns=["person:new:", "person-new-"],
    ),
    "group": EntityType(
        name="group",
        prefix="group-",
        description=(
            "A chat group, team, or named collective of people. e.g. a WhatsApp group, a family chat."
        ),
        keywords=[],
        triggers_types=["group"],
        extraction_rules=[],
        reconciliation_rules="No specific reconciliation rules.",
        skip_expand=True,
    ),
    "location": EntityType(
        name="location",
        prefix="location-",
        description=(
            "A physical place: a city, a venue, a house, a restaurant, a hotel. "
            "Not abstract locations like 'the cloud' or 'the internet'."
        ),
        keywords=["villa", "hotel", "restaurant", "house"],
        triggers_types=["location"],
        extraction_rules=[],
        reconciliation_rules="No specific reconciliation rules.",
        follow_for_bulletins=True,
    ),
    "trip": EntityType(
        name="trip",
        prefix="trip-",
        description=(
            "A planned or completed trip/holiday. Contains stays (individual accommodation legs within the trip). "
            "The trip-level start_date/end_date cover the overall trip window. "
            "Do NOT set destination on the trip — destinations come from the individual stay accommodation locations."
        ),
        keywords=["trip", "travel", "holiday", "vacation", "flight"],
        triggers_types=["trip", "stay", "location"],
        extraction_rules=[
            "When a bulletin describes flights, trains, buses, ferries, or other transport with specific "
            "routes, times, flight numbers, or booking references, create a connection entity for each "
            "individual HOP (one direct leg from A to B). A multi-leg journey under one booking/PNR "
            "becomes SEPARATE connection entities — one per leg. e.g. PER→KUL→LHR→GVA is three connections. "
            "Give each connection a descriptive slug (e.g. connection-perth-kuala-lumpur-mh124, "
            "connection-kuala-lumpur-london-mh002, connection-london-geneva-ba744). "
            "They share the same booking_ref. Extract structured claims on each connection: "
            "departure_location, arrival_location, departure_time, arrival_time, transport_type, "
            "duration, booking_ref, route, passengers. "
            "Then add a connection claim on the trip for EACH connection entity. "
            "NEVER skip transport data — it is as important as person or trip data.",
        ],
        reconciliation_rules=(
            "1. Stay date ranges must not overlap.\n"
            "2. Each distinct accommodation (different hotel, different city, or non-contiguous dates at the "
            "same city) MUST be its own separate stay entity.\n"
            "3. If two stays reference the same location with contiguous/overlapping "
            "dates, they should be merged into one stay.\n"
            "4. The trip should have at least one stay.\n"
            "5. The trip should NOT have destination, start_date, or end_date claims — "
            "those are derived from the stays.\n"
            "6. When you create_entity or delete_entity for a stay, you MUST also "
            "update this trip's leg claims: retract leg claims referencing deleted "
            "stays, add leg claims referencing newly created stays.\n"
            "7. All currently referenced stays in the leg claims must actually exist "
            "as active entities. If a leg claim references a non-existent or archived "
            "stay, retract that claim.\n"
            "8. Connection claims should reference connection entities whose departure_time falls "
            "within the trip's overall date range (derived from stay arrival_date/departure_date). "
            "If a connection's departure_time is outside this range, raise a question.\n"
            "9. If two connections have the same departure_time and route, they may be duplicates — "
            "raise a question rather than silently fixing."
        ),
        follow_for_bulletins=True,
    ),
    "stay": EntityType(
        name="stay",
        prefix="stay-",
        description=(
            "An accommodation leg within a trip — one hotel, Airbnb, villa, or other place you sleep. "
            "Each stay has its own arrival_date/departure_date and an accommodation location. "
            "CRITICAL: each distinct accommodation (different hotel, different city, or different date range "
            "at the same city) MUST be its own separate stay entity. Two nights in different "
            "hotels within Paris are two stays, not one. Include location and date range in the "
            "slug for uniqueness (e.g. stay-paris-june12-14, stay-paris-june14-16)."
        ),
        keywords=["stay", "hotel", "airbnb", "villa", "accommodation", "check-in", "check-out"],
        triggers_types=["stay"],
        extraction_rules=[
            "Each stay represents ONE accommodation booking. If a trip involves multiple hotels, "
            "create a separate stay entity for each hotel — even if they are in the same city. "
            "A stay at Hotel A on June 1-3 and a stay at Hotel B on June 3-5 are two different stay entities.",
        ],
        reconciliation_rules=(
            "1. Arrival date must be before departure date.\n"
            "2. There MUST be exactly one arrival_date claim and exactly one departure_date claim. "
            "If there are duplicates, keep the most specific/sourced one and retract the rest."
        ),
        follow_for_bulletins=True,
    ),
    "attraction": EntityType(
        name="attraction",
        prefix="attraction-",
        description=(
            "A thing to see or do on a trip: a temple, museum, beach, market, viewpoint, "
            "restaurant, bar, activity, or tour. Each attraction is a distinct place or activity "
            "worth visiting. Include location and trip context in the slug for uniqueness "
            "(e.g. attraction-tanah-lot, attraction-mama-san-dinner)."
        ),
        keywords=["visit", "see", "do", "attraction", "sightseeing", "temple", "museum",
                   "beach", "market", "viewpoint", "activity", "tour"],
        triggers_types=["attraction"],
        extraction_rules=[
            "Each attraction is a distinct place or activity to visit. Create separate attraction "
            "entities for different places even if they're in the same area. "
            "If the bulletin mentions a specific location for the attraction, create both the "
            "attraction entity and reference the location.",
        ],
        reconciliation_rules=(
            "1. If the attraction has no associated_trip claim, check if any trip references it "
            "in its attraction claims. If found, add the associated_trip claim.\n"
            "2. If two attractions have the same location and visit_date, they may be duplicates — "
            "raise a question rather than silently fixing."
        ),
        follow_for_bulletins=True,
    ),
    "dayplan": EntityType(
        name="dayplan",
        prefix="dayplan-",
        description=(
            "A planned itinerary for a single day of a trip. Collects notes, ideas, and attractions "
            "for a specific date. Include the trip and date in the slug for uniqueness "
            "(e.g. dayplan-bali-aug3, dayplan-paris-jun12). Created when planning days of a trip."
        ),
        keywords=["day plan", "itinerary", "plan for", "agenda"],
        triggers_types=["dayplan"],
        extraction_rules=[
            "Each dayplan covers exactly one date. Create separate dayplan entities for each day. "
            "Include attraction references for things planned for that day, and notes for ideas or free-text plans.",
        ],
        reconciliation_rules=(
            "1. There MUST be exactly one date claim. If there are duplicates, keep the most specific one.\n"
            "2. Attraction claims should reference existing attraction entities. If an attraction claim "
            "references a non-existent entity, raise a question.\n"
            "3. If the dayplan has no associated_trip, check if any trip references it in its dayplan claims. "
            "If found, add the associated_trip claim."
        ),
        follow_for_bulletins=True,
    ),
    "connection": EntityType(
        name="connection",
        prefix="connection-",
        description=(
            "A single transport hop: one flight leg, one train ride, one bus segment. "
            "Each hop is its own connection entity, even if multiple hops share the same booking/PNR. "
            "Has departure/arrival details, transport type, and booking references."
        ),
        keywords=["flight", "train", "bus", "ferry", "Eurostar", "booking ref"],
        triggers_types=["connection"],
        extraction_rules=[
            "Each connection is ONE HOP (one direct leg from A to B). A multi-leg journey "
            "under one booking/PNR becomes SEPARATE connection entities — one per leg. "
            "e.g. PER→KUL→LHR→GVA is three connections, not one. They share the same booking_ref.",
        ],
        reconciliation_rules=(
            "1. The connection SHOULD be referenced by a trip's ``connection`` claim. "
            "Use get_entity to check if any trip references this connection in its "
            "``connection`` claims (shown in reverse references). If no trip references "
            "this connection, use list_entities(\"trip\") to find candidate trips, then "
            "get_entity to inspect their date ranges (derived from stays). If exactly one "
            "trip's date range encompasses this connection's departure_time, add a "
            "``connection`` claim on that trip with object_id set to this connection's "
            "entity_id.\n"
            "2. There MUST be exactly one departure_location, one arrival_location, "
            "one departure_time, one arrival_time, one transport_type, one duration, "
            "and one route claim. If there are duplicates, keep the most specific/sourced "
            "version and retract the rest.\n"
            "3. If the connection represents a multi-hop journey (e.g. PER→KUL→LHR→GVA "
            "as a single entity), it should be split into separate connection entities, "
            "one per hop. Create the new entities, add connection claims on the trip, "
            "and delete the original multi-hop entity.\n"
            "4. If the connection has no departure_time but the source bulletins contain "
            "departure information, add the missing departure_time claim.\n"
            "5. If the connection cannot be linked to any trip and no suitable trip exists, "
            "raise a question asking which trip it belongs to."
        ),
        follow_for_bulletins=True,
        has_orphan_linker=True,
    ),
    "event": EntityType(
        name="event",
        prefix="event-",
        description=(
            "A planned event: a dinner, a party, a meeting, a concert. "
            "Has a time, a location, and attendees."
        ),
        keywords=["event", "dinner", "party", "meeting", "concert"],
        triggers_types=["event"],
        extraction_rules=[],
        reconciliation_rules=(
            "1. start_time must be before end_time.\n"
            "2. If associated_trip is set, the event should fall within the trip date range."
        ),
        follow_for_bulletins=True,
    ),
    "task": EntityType(
        name="task",
        prefix="task-",
        description=(
            "A task or todo item. Something that needs to be done. "
            "Has an owner, a status, and optionally a due date."
        ),
        keywords=["task", "todo", "need to", "remember to"],
        triggers_types=["task"],
        extraction_rules=[],
        reconciliation_rules="No specific reconciliation rules.",
        skip_new_patterns=["task:new:"],
    ),
    "file": EntityType(
        name="file",
        prefix="file-",
        description=(
            "A file or document in the workspace or accessible via URL. "
            "Every file entity MUST have a file_path claim with the actual workspace-relative "
            "path (e.g. 'docs/itinerary.md', 'src/main.py') or a full URL (https://...). "
            "Vague values like 'workspace', 'project', or 'root' are NOT valid file paths. "
            "Do NOT create file entities for things that are not actual files."
        ),
        keywords=["file", "document", "spreadsheet", "pdf", ".md", ".txt", "sheet", "folder", "path", "wrote to", "saved to"],
        triggers_types=["file"],
        extraction_rules=[
            "file entities: only create a file entity if the bulletin contains an actual workspace path "
            "(e.g. 'docs/itinerary.md') or URL (https://...). If the bulletin mentions a file but gives no "
            "path, do NOT create a file entity — skip it. Vague values like 'workspace', 'project', "
            "'new file', or bare filenames without directory separators are NOT valid paths. "
            "No valid path = no file entity. Never create file entities for abstract concepts.",
        ],
        reconciliation_rules="No specific reconciliation rules.",
        skip_expand=True,
        skip_new_patterns=["file:new:"],
    ),
    "thing": EntityType(
        name="thing",
        prefix="thing-",
        description=(
            "A tangible physical object, animal, or product. "
            "Must have a thing_type claim (animal, tool, vehicle, toy, device, furniture, food, appliance, etc.). "
            "Not for abstract concepts, software, or services."
        ),
        keywords=["bought", "purchased", "owns", "bike", "car", "toy", "tool",
                   "animal", "pet", "device", "phone", "laptop", "ebike", "motor"],
        triggers_types=["thing"],
        extraction_rules=[
            "thing entities are physical objects and animals only. Not for abstract concepts.",
        ],
        reconciliation_rules="No specific reconciliation rules.",
        skip_new_patterns=["thing:new:"],
    ),
    "decision": EntityType(
        name="decision",
        prefix="decision-",
        description=(
            "A decision that was made. Has a decider (person) and a rationale. "
            "Should reference the entity it's about via related_entity."
        ),
        keywords=["decided", "decision", "going with"],
        triggers_types=["decision"],
        extraction_rules=[],
        reconciliation_rules="No specific reconciliation rules.",
    ),
}

# Derived constants — computed from the registry for backward compatibility.
ENTITY_TYPES: tuple[str, ...] = tuple(ENTITY_TYPE_REGISTRY.keys())
ENTITY_TYPE_PREFIXES: tuple[str, ...] = tuple(et.prefix for et in ENTITY_TYPE_REGISTRY.values())
ENTITY_TYPE_DESCRIPTIONS: dict[str, str] = {name: et.description for name, et in ENTITY_TYPE_REGISTRY.items()}
FOLLOW_FOR_BULLETINS_PREFIXES: tuple[str, ...] = tuple(
    et.prefix for et in ENTITY_TYPE_REGISTRY.values() if et.follow_for_bulletins
)
SKIP_NEW_PATTERNS: tuple[str, ...] = tuple(
    p for et in ENTITY_TYPE_REGISTRY.values() if et.skip_new_patterns for p in et.skip_new_patterns
)


def detect_entity_type(entity_id: str) -> str:
    """Determine entity type from an entity ID using the registry."""
    for et in ENTITY_TYPE_REGISTRY.values():
        if entity_id.startswith(et.prefix):
            return et.name
    colon = entity_id.find(":")
    if colon > 0 and entity_id[:colon] in ENTITY_TYPE_REGISTRY:
        return entity_id[:colon]
    return "person"


def detect_entity_types_in_text(text: str) -> list[str]:
    """Detect likely entity types mentioned in text using registry keywords."""
    types: list[str] = ["person"]
    lower = text.lower()
    for et in ENTITY_TYPE_REGISTRY.values():
        if et.keywords and any(w in lower for w in et.keywords):
            types.extend(et.triggers_types)
    return list(set(types))


# Claim types where object_id is an entity reference (not a scalar value).
# Used by render_entity_full to decide which claims to expand recursively.
ENTITY_REF_CLAIM_KEYS: frozenset[str] = frozenset({
    "spouse", "parent", "child", "sibling", "grandparent", "grandchild",
    "member", "location", "organizer", "attendee", "associated_trip",
    "parent_location", "associated_contact", "leg", "accommodation",
    "owner", "related_entity", "decider", "file_ref", "attraction",
    "connection", "passenger", "dayplan",
})


def get_claim_types_for_entity(entity_type: str) -> list[ClaimType]:
    """Return claim types applicable to a given entity type."""
    return [ct for ct in CLAIM_TYPE_REGISTRY.values() if entity_type in ct.applicable_types]


def get_all_keys() -> set[str]:
    """Return all valid claim type keys."""
    return set(CLAIM_TYPE_REGISTRY.keys())


# ---------------------------------------------------------------------------
# Entity type rendering templates (display order + labels)
# ---------------------------------------------------------------------------

# Defines the order and labels for rendering each entity type's claims.
_ENTITY_TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "person": [
        ("alias", "Also known as"),
        ("appearance", "Appearance"),
        ("spouse", "Spouse/Partner"),
        ("parent", "Parent"),
        ("child", "Child"),
        ("sibling", "Sibling"),
        ("grandparent", "Grandparent"),
        ("grandchild", "Grandchild"),
        ("home_address", "Home"),
        ("workplace", "Workplace"),
        ("job", "Job"),
        ("birthday", "Birthday"),
        ("hometown", "Hometown"),
        ("language", "Languages"),
        ("contact_method", "Contact"),
        ("food_preference", "Food"),
        ("drink_preference", "Drinks"),
        ("dietary_restriction", "Dietary"),
        ("interest", "Interests"),
        ("personality", "Personality"),
        ("preference", "Preferences"),
        ("contact_id", "Contact ID"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "group": [
        ("alias", "Also known as"),
        ("purpose", "Purpose"),
        ("vibe", "Vibe"),
        ("member", "Members"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "event": [
        ("name", "Event"),
        ("start_time", "Starts"),
        ("end_time", "Ends"),
        ("location", "Location"),
        ("organizer", "Organizer"),
        ("attendee", "Attendees"),
        ("recurrence", "Recurrence"),
        ("associated_trip", "Trip"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "location": [
        ("alias", "Also known as"),
        ("location_type", "Type"),
        ("address", "Address"),
        ("interest", "Highlights"),
        ("opening_hours", "Hours"),
        ("parent_location", "Part of"),
        ("associated_contact", "Associated with"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "trip": [
        ("purpose", "Purpose"),
        ("member", "Members"),
        ("leg", "Legs"),
        ("attraction", "Attractions"),
        ("dayplan", "Day plans"),
        ("connection", "Connections"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "attraction": [
        ("attraction_type", "Type"),
        ("location", "Location"),
        ("visit_date", "Visiting"),
        ("cost", "Cost"),
        ("associated_trip", "Trip"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "dayplan": [
        ("date", "Date"),
        ("notes", "Notes"),
        ("attraction", "Attractions"),
        ("associated_trip", "Trip"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "stay": [
        ("accommodation", "Accommodation"),
        ("accommodation_type", "Type"),
        ("accommodation_address", "Address"),
        ("arrival_date", "Arriving"),
        ("departure_date", "Departing"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "connection": [
        ("transport_type", "Type"),
        ("departure_location", "From"),
        ("arrival_location", "To"),
        ("departure_time", "Departs"),
        ("arrival_time", "Arrives"),
        ("duration", "Duration"),
        ("route", "Route"),
        ("booking_ref", "Booking ref"),
        ("passenger", "Passengers"),
        ("seat", "Seat"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "task": [
        ("name", "Task"),
        ("description", "Description"),
        ("owner", "Owner"),
        ("task_status", "Status"),
        ("due_date", "Due"),
        ("related_entity", "Related to"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "file": [
        ("name", "File"),
        ("file_path", "Path"),
        ("purpose", "Purpose"),
        ("owner", "Owner"),
        ("related_entity", "Related to"),
        ("file_ref", "Related files"),
        ("truth", "User truth"),
    ],
    "thing": [
        ("name", "Thing"),
        ("thing_type", "Type"),
        ("description", "Description"),
        ("owner", "Owner"),
        ("location", "Location"),
        ("related_entity", "Related to"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
    "decision": [
        ("decider", "Decided by"),
        ("rationale", "Rationale"),
        ("related_entity", "About"),
        ("file_ref", "Files"),
        ("truth", "User truth"),
    ],
}

# ---------------------------------------------------------------------------
# Jinja2 entity template engine
# ---------------------------------------------------------------------------

_ENTITY_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "entity_templates")
_jinja_env = Environment(
    loader=FileSystemLoader(_ENTITY_TEMPLATES_DIR),
    keep_trailing_newline=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def _group_claims(claims: list[dict[str, Any]]) -> dict[str, list[dict]]:
    by_type: dict[str, list[dict]] = {}
    for claim in claims:
        key = claim["claim_type_key"]
        by_type.setdefault(key, []).append(claim)
    return by_type


def _build_template_context(
    entity_type: str,
    display_name: str,
    claims: list[dict[str, Any]],
    entity_id: str | None = None,
) -> dict:
    by_type = _group_claims(claims)
    template = _ENTITY_TEMPLATES.get(entity_type, [])
    template_keys = {ck for ck, _ in template}
    orphans = {k: v for k, v in by_type.items() if k not in template_keys}

    return {
        "display_name": display_name,
        "entity_id": entity_id,
        "entity_type": entity_type,
        "claims": by_type,
        "orphans": orphans,
        "rendered_refs": {},
    }


async def _resolve_entity_refs(
    db: Any,
    claims: list[dict[str, Any]],
    visited: set[str],
) -> dict[str, dict]:
    resolved: dict[str, dict] = {}
    for claim in claims:
        ref = claim.get("object_id") or claim.get("value")
        if not ref or not isinstance(ref, str) or ref in visited or ref in resolved:
            continue
        if not any(ref.startswith(p) for p in ENTITY_TYPE_PREFIXES):
            continue
        row = await db.fetch_one(
            "SELECT entity_id, entity_type, display_name FROM memory_entities "
            "WHERE entity_id = ? AND status = 'active'",
            (ref,),
        )
        if not row:
            continue
        ref_claims = await db.fetch_all(
            "SELECT claim_type_key, object_id, value FROM memory_claims "
            "WHERE status = 'active' AND subject_id = ?",
            (ref,),
        )
        claim_dicts = [
            {"claim_type_key": r["claim_type_key"], "object_id": r["object_id"], "value": r["value"]}
            for r in ref_claims
        ]
        resolved[ref] = {
            "entity_id": row["entity_id"],
            "entity_type": row["entity_type"],
            "display_name": row["display_name"],
            "claims": _group_claims(claim_dicts),
            "claim_dicts": claim_dicts,
        }
    return resolved


async def render_entity(
    entity_type: str,
    display_name: str,
    claims: list[dict[str, Any]],
    entity_id: str | None = None,
    *,
    db: Any = None,
    _visited: set[str] | None = None,
) -> str:
    """Render entity claims into a human-readable text block.

    Uses Jinja2 templates from entity_templates/ if available, otherwise
    falls back to the generic renderer. When db is provided, entity
    references are resolved recursively for rich rendering.
    """
    if _visited is None:
        _visited = set()
    if entity_id:
        _visited = _visited | {entity_id}

    # Try Jinja2 template for this entity type
    template_name = f"{entity_type}.md"
    try:
        template = _jinja_env.get_template(template_name)
    except TemplateNotFound:
        return _render_entity_generic(entity_type, display_name, claims, entity_id)

    ctx = _build_template_context(entity_type, display_name, claims, entity_id)

    # Pre-resolve and render entity references recursively
    resolved: dict[str, dict] = {}
    rendered_refs: dict[str, str] = {}
    if db:
        resolved = await _resolve_entity_refs(db, claims, _visited)
        for eid, entity_data in resolved.items():
            rendered_refs[eid] = await render_entity(
                entity_data["entity_type"],
                entity_data["display_name"],
                entity_data["claim_dicts"],
                entity_id=eid,
                db=db,
                _visited=_visited,
            )

    # Build sort_by_date closure that captures the resolved dict
    def sort_by_date(entity_ids: list[str], date_keys: list[str] | None = None) -> list[str]:
        keys = date_keys or ["departure_time", "arrival_date", "start_time", "departure_date"]
        def sort_key(eid: str) -> str:
            entity = resolved.get(eid)
            if not entity:
                return ""
            for key in keys:
                cs = entity.get("claims", {}).get(key, [])
                if cs:
                    return cs[0].get("value", "")
            return ""
        return sorted(entity_ids, key=sort_key)

    ctx["resolved"] = resolved
    ctx["rendered_refs"] = rendered_refs
    ctx["sort_by_date"] = sort_by_date
    ctx["first_claim"] = _first_claim

    return template.render(**ctx)


def _first_claim(entity: dict, key: str, default: str = "") -> str:
    claims = entity.get("claims", {}).get(key, [])
    return claims[0].get("value", "") if claims else default


def _render_entity_generic(
    entity_type: str,
    display_name: str,
    claims: list[dict[str, Any]],
    entity_id: str | None = None,
) -> str:
    """Render entity claims into a human-readable text block using templates.

    claims: list of dicts with keys: claim_type_key, object_id, value
    entity_id: if provided, claims where this entity is the object_id will
               show the subject_id instead (avoids self-referencing display).
    """
    by_type: dict[str, list[str]] = {}
    for claim in claims:
        key = claim["claim_type_key"]
        obj = claim.get("object_id")
        val = claim.get("value")
        # When entity is the object, show the subject instead of itself
        if entity_id and obj == entity_id and val is None:
            val = claim.get("subject_id")
        by_type.setdefault(key, []).append(val or obj or "")

    template = _ENTITY_TEMPLATES.get(entity_type, [])
    lines: list[str] = [display_name]

    for claim_key, label in template:
        values = by_type.get(claim_key)
        if not values:
            continue
        if len(values) == 1:
            lines.append(f"{label}: {values[0]}")
        else:
            lines.append(f"{label}:")
            for v in values:
                lines.append(f"  - {v}")

    # Append orphan claims (claim types not in the template)
    template_keys = {ck for ck, _ in template}
    orphan_keys = [k for k in by_type if k not in template_keys]
    if orphan_keys:
        lines.append("Orphan claims:")
        for k in sorted(orphan_keys):
            vals = by_type[k]
            if len(vals) == 1:
                lines.append(f"  {k}: {vals[0]}")
            else:
                lines.append(f"  {k}:")
                for v in vals:
                    lines.append(f"    - {v}")

    return "\n".join(lines)


def build_extraction_prompt_section(entity_types: list[str]) -> str:
    """Build the entity type glossary and claim types section for the extraction prompt.

    Groups claim types under their entity types with descriptions.
    Appends per-type extraction rules from the registry.
    """
    sections: list[str] = ["## Entity Types\n"]

    for etype in entity_types:
        et_def = ENTITY_TYPE_REGISTRY.get(etype)
        desc = et_def.description if et_def else ""
        sections.append(f"### {etype}")
        if desc:
            sections.append(desc)
        sections.append("")

        types = get_claim_types_for_entity(etype)
        if types:
            sections.append("Claim types:")
            for ct in types:
                sections.append(f"  - {ct.key}: {ct.description}")

        if et_def and et_def.extraction_rules:
            sections.append("")
            for rule in et_def.extraction_rules:
                sections.append(rule)

        sections.append("")

    return "\n".join(sections)

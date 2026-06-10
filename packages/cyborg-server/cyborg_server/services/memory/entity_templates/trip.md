{{ display_name }}
{% if claims.get("purpose") %}Purpose: {{ claims.purpose[0].value }}
{% endif %}
{% if claims.get("member") %}
Members: {{ claims.member | map(attribute="value") | join(", ") }}
{% endif %}
{% set leg_ids = claims.get("leg", []) | map(attribute="object_id") | select("string") | list %}
{% set conn_ids = claims.get("connection", []) | map(attribute="object_id") | select("string") | list %}
{% set timeline_ids = sort_by_date(leg_ids + conn_ids) %}
{% if timeline_ids %}
Itinerary:
{% for eid in timeline_ids %}
{% if rendered_refs.get(eid) %}
{{ rendered_refs[eid] | indent(2) }}
{% endif %}
{% endfor %}
{% endif %}
{% if claims.get("attraction") %}
Attractions:
{% for a in claims.attraction %}  - {{ a.value or a.object_id }}
{% endfor %}
{% endif %}
{% if orphans %}
{% for key, vals in orphans | dictsort %}
{% if vals | length == 1 %}{{ key }}: {{ vals[0].value or vals[0].object_id }}
{% else %}{{ key }}:
{% for v in vals %}  - {{ v.value or v.object_id }}
{% endfor %}
{% endif %}
{% endfor %}
{% endif %}
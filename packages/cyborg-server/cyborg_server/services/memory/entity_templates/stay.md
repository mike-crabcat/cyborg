{{ display_name }} [{{ entity_id }}]
{% for c in claims.get("accommodation", []) %}Accommodation: {{ resolved.get(c.object_id, {}).get("display_name", c.value or c.object_id) if c.object_id else c.value }}
{% endfor %}
{% for c in claims.get("accommodation_type", []) %}Type: {{ c.value }}
{% endfor %}
{% for c in claims.get("accommodation_address", []) %}Address: {{ c.value }}
{% endfor %}
{% for c in claims.get("arrival_date", []) %}Arriving: {{ c.value }}
{% endfor %}
{% for c in claims.get("departure_date", []) %}Departing: {{ c.value }}
{% endfor %}
{% if orphans %}
{% for key, vals in orphans | dictsort %}
{% if vals | length == 1 %}{{ key }}: {{ vals[0].value or vals[0].object_id }}
{% else %}{{ key }}:
{% for v in vals %}  - {{ v.value or v.object_id }}
{% endfor %}
{% endif %}
{% endfor %}
{% endif %}
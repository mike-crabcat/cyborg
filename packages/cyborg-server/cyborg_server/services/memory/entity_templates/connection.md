{{ display_name }} [{{ entity_id }}]
{% for c in claims.get("transport_type", []) %}Type: {{ c.value }}
{% endfor %}
{% for c in claims.get("departure_location", []) %}From: {{ c.value }}
{% endfor %}
{% for c in claims.get("arrival_location", []) %}To: {{ c.value }}
{% endfor %}
{% for c in claims.get("departure_time", []) %}Departs: {{ c.value }}
{% endfor %}
{% for c in claims.get("arrival_time", []) %}Arrives: {{ c.value }}
{% endfor %}
{% for c in claims.get("duration", []) %}Duration: {{ c.value }}
{% endfor %}
{% for c in claims.get("booking_ref", []) %}Booking: {{ c.value }}
{% endfor %}
{% for c in claims.get("passenger", []) %}Passenger: {{ c.value or c.object_id }}
{% endfor %}
{% for c in claims.get("seat", []) %}Seat: {{ c.value }}
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
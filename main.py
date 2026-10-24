import hashlib
import json
import os
import sys
import time
import zoneinfo
from datetime import datetime
from pathlib import Path
from uuid import UUID

import dateparser
import requests
import toml
from bs4 import BeautifulSoup
from icalendar import Calendar, Event, vCalAddress

CALENDAR_UUID = UUID("0c4375e0-9b48-4c7c-92ba-c40495bef667")

tz = zoneinfo.ZoneInfo("Europe/Paris")

credentials = toml.load(
    os.environ.get("UTOPIALES_CREDENTIALS", "utopiales-credentials.toml")
)
data = Path("data")

data.mkdir(parents=True, exist_ok=True)

s = requests.Session()
s.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:159.0) Gecko/20100101 Firefox/159.0"
    }
)


try:
    with open(data / "cache.json", "r") as f:
        cache = json.load(f)
except (FileNotFoundError, json.decoder.JSONDecodeError):
    cache = {}


places = {place["name"]: place for place in toml.load("places.toml")["place"]}

elem_text_or_none = lambda e: e.text if e is not None else None


def capfirst(s: str) -> str:
    return s[:1].upper() + s[1:]


def ensure_datetime(d):
    """
    Converts d to a datetime object.
    :param d: Either a datetime object or a ISO 8601 datetime string.
    :return: A datetime object.
    """
    return d if type(d) is datetime else datetime.fromisoformat(str(d))


def get_paragraphs(soup, selector) -> str:
    return "\n\n".join(
        filter(
            lambda e: e,  # filters out empty paragraphs
            map(lambda elem: elem.text.strip(), soup.select(selector)),
        )
    ).strip()


def get_actor(actor_link):
    if actor_link in cache:
        return cache[actor_link]

    r = s.get(actor_link)
    soup = BeautifulSoup(r.content, "html.parser")

    category_element = soup.select_one(".entry__content .uto__terms")
    category = None
    if category_element:
        if category_element.name == "p":  # single category
            category = elem_text_or_none(category_element)
        elif category_element.name == "ul":
            category = " – ".join(map(elem_text_or_none, category_element.select("li")))

    actor = {
        "name": elem_text_or_none(soup.select_one("h1")),
        "link": actor_link,
        "category": category,
        "description": get_paragraphs(
            soup, ".entry__content .wp-block-paragraph, .entry__content .wp-block-list"
        ),
    }

    time.sleep(0.5)

    cache[actor_link] = actor
    return actor


def get_event(event_link: str) -> dict | None:
    if event_link in cache:
        return cache[event_link]

    r = s.get(event_link)
    soup = BeautifulSoup(r.content, "html.parser")

    # Extract all metadata from sidebar
    metadata = {}
    for metadata_td_element in soup.select(".card__event td"):
        spans = metadata_td_element.find_all("span")
        if len(spans) == 2:
            metadata[spans[0].text.lower()] = (spans[1].text,)
        else:  # link in metadata (moderator)
            metadata_link = metadata_td_element.find("a")
            if metadata_link:
                metadata[spans[0].text.lower()] = (
                    metadata_link.text,
                    metadata_link.attrs["href"],
                )

    if "heure" not in metadata or "date" not in metadata:
        return None

    # We add explicit timezones, else the ics module uses UTC.
    event_hour_parts = metadata["heure"][0].split(" ")
    begin = dateparser.parse(f"{metadata['date'][0]} {event_hour_parts[1]}").replace(
        tzinfo=tz
    )
    end = dateparser.parse(f"{metadata['date'][0]} {event_hour_parts[3]}").replace(
        tzinfo=tz
    )

    event = {
        "title": elem_text_or_none(soup.select_one("h1")),
        "description": get_paragraphs(
            soup,
            ".entry__content p, .entry__content ul, .entry__content h2, .entry__content h3",
        ),
        "begin": begin,
        "end": end,
        "location": metadata["lieu"][0] if "lieu" in metadata else None,
        "room": metadata["salle"][0] if "lieu" in metadata else None,
        "people": [
            get_actor(guest_link_element.attrs["href"])
            for guest_link_element in soup.select(".list__guests .entry__permalink")
        ],
        "moderator": (
            get_actor(metadata["moderateur"][1]) if "moderateur" in metadata else None
        ),
        "type": elem_text_or_none(
            soup.select_one(".entry__content .uto__event-intro .uto__terms")
        ),
        "domain": elem_text_or_none(
            soup.select_one(".entry__content .uto__event-intro .uto__parcours__caption")
        ),
        "link": event_link,
    }

    time.sleep(0.5)

    cache[event_link] = event
    return event


# Login ------------------------------------------------------------------------

print("Logging in…")

# Login 1/2: get nonce from login form

r = s.get("https://www.utopiales.org/mon-compte/")
soup = BeautifulSoup(r.content, "html.parser")
nonce = soup.select("#woocommerce-login-nonce")[0].attrs["value"]

# Login 2/2: login

# We request the selection page so the returned HTML content after login is
# what we need to build the ICS file, saving us one request.
r = s.post(
    "https://www.utopiales.org/mon-compte/ma-selection/",
    data={
        "username": credentials["username"],
        "password": credentials["password"],
        "woocommerce-login-nonce": nonce,
        "_wp_http_referer": "/mon-compte/ma-selection/",
        "login": "Se connecter",
    },
)

if not r.ok:
    print(f"Login failed – {r.status_code}")
    sys.exit()


# Extract events & build ICS ---------------------------------------------------

soup = BeautifulSoup(r.content, "html.parser")
program = soup.select("table a[href]:not(.button)")

calendar = Calendar.new(
    name="Utopiales",
    organization="utopiales-ics-carrade-eu",
    uid=CALENDAR_UUID,
    language="FR",
)

for link_element in program:
    print(f"Processing {link_element.text} — {link_element['href']}")

    # Load event (from cache or scrapping)

    event = get_event(str(link_element["href"]))
    if not event:
        print(f"Skipping–Invalid page (no date in page)")
        continue

    # Build ICS event (with description and all) for this event.

    prefix = ""
    event_type = str(event["type"])
    if "Ateliers" in event_type:
        prefix += "🧠"
    if "Cinéma" in event_type:
        prefix += "🎞️"
    if "Créations sonores" in event_type:
        prefix += "🔊"
    if "Jeux" in event_type:
        prefix += "🎲"
    if "Performance" in event_type:
        prefix += "🎨"
    if "Programme jeunesse" in event_type:
        prefix += "🧒"
    if "Soirées" in event_type:
        prefix += "🎭"

    description = str(event["type"]) or ""
    if event["domain"]:
        description += f" – {event['domain']}"

    description += f"\n\n{event['description']}"

    attendees = []
    organizer = None

    if event["people"] or event["moderator"]:
        # if only one attendee (incl. moderator)
        if len(event["people"]) + (1 if event["moderator"] else 0) == 1:
            only_attendee = event["people"][0] if event["people"] else event["moderator"]
            description += f"\n\nAvec {only_attendee['name']}"

        else:
            # we could inline but black doesn't parse it
            people_names = "\n".join(map(lambda p: f'— {p["name"]}', event["people"]))
            description += f"\n\nAvec\n{people_names.strip()}"

            if event["moderator"]:
                description += f"\n— {event['moderator']['name']} (modérateurice)"

        description += "\n\n\n————————\n\n"
        for actor in [*event["people"], event["moderator"]]:
            if not actor:
                continue

            description += f"\n{actor['name'].upper()}\n"
            if actor["category"]:
                description += f"{actor['category']}\n"
            description += f"\n{actor['description']}\n"
            description += f"\n→ {actor['link']}\n\n"

        for actor in event["people"]:
            attendees.append(
                vCalAddress(
                    actor["link"],
                    params={
                        "CN": actor["name"],
                        "ROLE": "REQ-PARTICIPANT",
                        "PARTSTAT": "ACCEPTED"
                    },
                )
            )

        if event["moderator"]:
            organizer = vCalAddress(
                event["moderator"]["link"],
                params={
                    "CN": event["moderator"]["name"],
                },
            )

    geo_place = places.get(event["location"])
    location = f"{(event['room'] + ', ') if event['room'] and event['room'] != event['location'] else ''}{event['location']}{(', ' + geo_place['address']) if geo_place else ''}"

    ical_event = Event.new(
        summary=f"{prefix} {capfirst(event['title'])}".strip(),
        description=description.strip(),
        start=ensure_datetime(event["begin"]),
        end=ensure_datetime(event["end"]),
        location=location,
        url=event["link"],
        attendees=attendees,
        organizer=organizer,
        uid=f"{hashlib.sha256(event['link'].encode()).hexdigest()}@utopiales.carrade.eu",
    )

    if geo_place:
        ical_event.add("GEO", (geo_place["lat"], geo_place["long"]))

        # Support for displayed map & “notify when to leave” in Apple Calendar
        ical_event.add(
            "X-APPLE-STRUCTURED-LOCATION",
            f"geo:{geo_place['lat']},{geo_place['long']}",
            parameters={
                "VALUE": "URI",
                "X-ADDRESS": location,
                "X-APPLE-RADIUS": "72",
                "X-TITLE": location,
            },
        )

    calendar.add_component(ical_event)


# Save -------------------------------------------------------------------------

with open(data / credentials["ics_generated_file_name"], "wb") as f:
    f.write(calendar.to_ical())

with open(data / "cache.json", "w") as f:
    json.dump(cache, f, indent=2, default=str)

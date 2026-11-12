# Flight Watch

Get a phone notification when **everyone in your group** can fly round trip to
the **same place on the same dates**, each for under a price you choose.

You list where people fly from, where you'd be happy to meet, a price cap and a
trip length. Twice a week it checks Google Flights prices for every departure
date in roughly the next six months and sends a push through the free
[ntfy](https://ntfy.sh) app when it finds dates that work for everybody.

It runs for free on GitHub. Your computer does not need to be on.

## Setup (about 15 minutes)

You need a free [GitHub](https://github.com) account and a free
[SerpApi](https://serpapi.com) account (250 searches a month, no card needed).

1. **Copy this repository.** Click **Use this template → Create a new
   repository**. Choose **Private**.
2. **Add your SerpApi key.** Copy your key from the SerpApi dashboard. In your
   new repository open **Settings → Secrets and variables → Actions → New
   repository secret**. Name it `SERPAPI_KEY` and paste the key as the value.
3. **Edit `config.json`** (click the file, then the pencil icon):

   | Setting | Meaning |
   |---|---|
   | `origins` | Airport codes people fly from. Put the most expensive airport first; it rules out bad options soonest and saves searches. |
   | `destinations` | Airport codes you'd be happy to meet at. An origin can also be a destination; the people who live there just don't fly. |
   | `max_price` | Highest round-trip price per person, in US dollars. |
   | `trip_nights` | Trip lengths to check, in nights. `[3]` is one length; `[2, 3]` checks both and uses twice the searches. |
   | `ntfy_topic` | Make up a long name nobody could guess, like `flights-kx82hq-lakehouse`. Anyone who knows it can read your alerts. |

   Commit the change.
4. **Get the notifications.** Install the ntfy app
   ([iOS](https://apps.apple.com/app/ntfy/id1625396347) /
   [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy)),
   tap **+** and subscribe to the exact topic name you chose. Everyone in the
   group can subscribe to the same topic.
5. **Try it.** In your repository open **Actions → Flight Watch → Run
   workflow**. In a minute or two you should get a push: either the deals it
   found or a note that nothing matches yet.

After that it runs by itself on Tuesdays and Fridays.

## What you'll receive

- **New deals:** one push listing each destination, the dates, and every
  person's price, with whether the outbound flight is nonstop and a Google
  Flights link per person.
- **Nothing new:** one short summary of the dates that currently match.
- A deal is only announced again if it gets cheaper.

## Staying inside the free 250 searches a month

Each run uses about one search per origin per destination, fewer when an early
origin rules a destination out, plus a few to look up stops and airlines for new
deals. Four origins and eight destinations is typically 15 to 40 searches a run.
The script checks your remaining searches first and skips the run if there
aren't enough. To use fewer: list fewer destinations, check one trip length, or
run once a week (edit the `cron` line in
`.github/workflows/flight-watch.yml`; times are UTC).

## Optional settings

Add any of these to `config.json`:

| Setting | Meaning |
|---|---|
| `window_start`, `window_end` | Only consider trips between these dates (`"2027-01-01"`). By default it looks from today as far ahead as one search reaches, about 200 days. |
| `allow_origin_as_destination` | `false` to require everyone to fly, even to someone's home city. |
| `detail_lookups` | How many destinations get the stops and airline lookup per run (default 3). |
| `notify.ntfy_server` | Use a different ntfy server instead of `https://ntfy.sh`. |
| `notify.email_to` | Also send email. Needs `SMTP_USER` and `SMTP_PASS` secrets. |

**Cities with several airports.** An airport code only matches that airport. To
match a whole city (Phoenix including Mesa, say), write destinations as names
with Google's city ID, which is the "Freebase ID" on the city's
[Wikidata](https://www.wikidata.org) page:

```json
"destinations": { "Phoenix": "/m/0d35y", "Las Vegas": "LAS" },
"origin_cities": { "DEN": "/m/02cl1" }
```

`origin_cities` tells it which city ID is home for an origin airport, so that
origin is excused when the city is the destination.

## Good to know

- Prices come from Google Flights through SerpApi and can change by the time
  you book. Always check the link.
- Only exact trip lengths are checked: `[3]` finds Monday to Thursday or Friday
  to Monday, not a 2-night trip.
- GitHub pauses scheduled runs in repositories with no activity for 60 days and
  emails you first. Re-enabling is one click on the Actions tab.
- To test without sending anything, tick **Dry run** when you run the workflow.

## Running it on your own computer

```bash
cp .env.example .env        # then put your key in .env
python3 flight_watch.py config.json --no-notify
python3 -m unittest         # offline tests, no searches
```

Python 3.9 or newer, no packages to install.

# Job Agent

A local AI job search pipeline for macOS. It finds desired roles and scores each one against your background using Apple's on-device model (Apple Intelligence). Results go to `job_matches.xlsx`.

Nothing about you or the postings leaves your machine except the job-site searches themselves.

## How it works

```
Indeed / LinkedIn (JobSpy) ─┐
Greenhouse / Lever boards ──┤      SQLite      Apple on-device model      Excel
USAJobs / Adzuna APIs ──────┴──► dedup store ──► fit score + summary ──► job_matches.xlsx
```

Each run only scores postings it hasn't seen before, so daily runs are quick.

## Setup (about 30 minutes, most of it the Xcode download)

1. **Check the Mac.** You need macOS 26 or later on Apple silicon. Apple menu > About This Mac.

2. **Turn on Apple Intelligence:** System Settings > Apple Intelligence & Siri. Wait until its model
   has finished downloading. This replaces Ollama; there's nothing else to install for the model.

3. **Install Xcode** from the App Store (Apple's Python SDK needs the full app). Open it once to
   accept the agreement, then in Terminal:
   ```
   sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
   ```

4. **Install Python 3.12** with [Homebrew](https://brew.sh) (the Python built into macOS is too old):
   ```
   brew install python@3.12
   ```

5. **Unzip this folder** somewhere permanent, ideally `~/JobAgent` in your home folder. Avoid
   Documents, Desktop and Downloads: macOS blocks scheduled jobs from reading those.

6. **Run setup** in Terminal. It creates a private Python environment and installs the libraries:
   ```
   cd ~/JobAgent
   chmod +x setup.sh run.sh schedule.sh
   ./setup.sh
   ```
   "Apple on-device model: OK" at the end means scoring will work.

7. **Edit `config.yaml`** in TextEdit or any editor. Your profile is already filled in.
   To bring your history over, copy `jobs.db` and `job_matches.xlsx` from the Windows folder into
   this one; your Status and Notes carry over.

8. **Test run:** in Terminal, in the folder:
   ```
   ./run.sh --limit 15
   ```
   This collects postings and scores only 15, so you can check the scores look sensible.
   Then run `./run.sh` with no arguments to score the rest.

## The spreadsheet

- **Top Matches**: fit score of 5 or higher; fully remote anywhere, or any arrangement in
  Buffalo-Niagara; not marked Rejected or Not interested.
- **Buffalo-Niagara**: every local posting (remote, hybrid or onsite), best first.
- **All Jobs**: everything scored, with Excel filters on every column.
- New postings since the last run have their titles highlighted.
- **Status** has a dropdown (New, Interested, Applied, Interviewing, Offer, Rejected, Not interested),
  and **Notes** is free text. Both are kept between runs.
- **Close the file before the agent runs.** If Excel has it open, the agent saves a dated copy instead.

## Run it automatically every morning

In Terminal, in the folder:

```
./schedule.sh install 07:45    # daily at 7:45 AM (uses launchd, the Mac's scheduler)
./schedule.sh status           # is it scheduled, last result
./schedule.sh now              # run the scheduled job right away to test it
./schedule.sh remove           # stop
```

If the Mac is asleep at 7:45, the run happens when it wakes. If it's shut down, that day is skipped.
The scheduled run's output goes to `logs/launchd.log`.

## Tuning

| Want to... | Change in `config.yaml` |
|---|---|
| Find different remote roles | `sources.jobspy.search_terms` |
| Find different local roles | `sources.jobspy_local.search_terms` |
| Widen or narrow the local radius | `sources.jobspy_local.distance_miles` |
| Count another town as local | `local_area_keywords` |
| See more or fewer Top Matches | `min_score_for_top` |
| Skip junk titles before scoring | `exclude_title_keywords` |
| Watch specific firms' career pages | enable `greenhouse` / `lever` and add company slugs |
| Federal roles (e.g. USPTO patent examiner) | enable `usajobs` and add a free API key |
| Re-score everything after editing your profile | run `./run.sh --rescore` |

**If scores feel off,** add more detail to `profile`: specific techniques, catalyst systems,
industries you'd like or want to avoid. That's the single biggest lever on quality.

## Upgrading from the first version

Replace `jobagent.py`, `config.yaml` and `README.md` with the new ones (copy your `[EDIT]` profile
lines across first). Keep `jobs.db` and `job_matches.xlsx`; your Status and Notes carry over.
Then run `./run.sh --rescore` once so older postings get the Local column and the new location rules.

## Speed

Apple's on-device model is smaller than the 14B model you ran on the 4070 Super (Apple describes it
as about 3 billion parameters), so expect a few seconds per posting and more borderline calls.
It reads at most about 4,000 tokens per posting, so the agent sends the first 3,500 characters of
each one and retries shorter if needed. Keep `profile` about its current length.
Now and then the model declines to rate a posting; those get a 5 and "Not rated by the model"
in Red flags so you can read them yourself.

## Troubleshooting

- **"Apple Intelligence model not available"**: turn it on in System Settings > Apple Intelligence
  & Siri and wait for the download to finish.
- **`pip install apple-fm-sdk` fails in setup**: usually Xcode and macOS versions don't match, or
  Xcode isn't selected (step 3). Update both and run `./setup.sh` again.
- **Scheduled run did nothing**: `cat logs/launchd.log`, and make sure the folder isn't in
  Documents, Desktop or Downloads.
- **JobSpy returns 0 results or errors for LinkedIn**: LinkedIn rate-limits quickly. Increase
  `delay_seconds`, lower `results_per_search`, or remove `linkedin` from `sites` for a day.
  Indeed is usually the most reliable.
- **Logs**: `logs/jobagent.log` has the full history of every run.

## A note on JobSpy

JobSpy scrapes Indeed and LinkedIn, which their terms of service don't allow. It's widely used for
personal job searches, but keep volumes modest. The Greenhouse, Lever, USAJobs and Adzuna sources
use official public APIs.

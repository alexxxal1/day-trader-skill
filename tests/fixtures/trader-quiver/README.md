# Quiver fixtures

`congress.html` and `insiders.html` are reduced, real public-page snapshots
retrieved on 2026-11-09 without a login or API key. Only the relevant headers
and selected records are retained. Their JavaScript literals deliberately
preserve the actual arrays/objects delivered by the public pages, including
the DIS negative-delay anomaly. They are historical fixtures, not live data.

`api-congress.json` and `api-insider.json` are **synthetic** schema fixtures.
They test common API field names, missing dates and inconsistent dates;
they are not claimed to be responses from an authenticated Quiver API call.

API paths and Token authentication were checked against the official client:
https://github.com/Quiver-Quantitative/python-api (quiverquant 0.2.6).
No third-party package is installed or required by this skill.

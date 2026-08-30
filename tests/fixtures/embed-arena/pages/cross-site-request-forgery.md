---
kind: summary
title: Cross‑Site Request Forgery (CSRF) Overview
identifiers: ["CSRF", "CVE-2008-6586", "μTorrent", "Netflix", "YouTube", "ING Direct", "McAfee Secure"]
entities: ["vulnerability: cross-site request forgery", "technique: CSRF token", "attack: confused deputy"]
claims: ["Cross-site request forgery | is a type of | malicious exploit of a website", "CSRF | exploits the trust that | a site has in a user's browser", "A CSRF attack | tricks | a user's browser into sending forged HTTP requests", "CSRF attacks | can cause | inadvertent data leakage, session state change, or account manipulation", "CSRF defenses | include | techniques that use header data, form data, or cookies"]
kind_of_source: article
action: Developers should implement CSRF tokens or other defenses to protect authenticated web actions from unauthorized forged requests.
source: 34e43435d1ee
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

Cross‑Site Request Forgery is a web security vulnerability where an attacker tricks a victim’s browser into sending unauthorized commands to a web application the victim is authenticated with. The attack exploits the browser’s automatic inclusion of cookies in requests, allowing actions to be performed without the user’s consent or knowledge. Historically, notable sites like Netflix, ING Direct, and YouTube have been vulnerable, leading to unauthorized account changes or data exposure. Defenses such as CSRF tokens aim to validate that requests originate from the application’s own forms. The attack is an example of a confused deputy, leveraging the browser’s privilege to perform unintended actions on the user’s behalf.

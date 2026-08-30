---
kind: summary
title: Clickjacking (UI Redressing)
identifiers:
  - clickjacking
  - UI redressing
  - likejacking
  - CursorJacking
  - X-Frame-Options
  - BeEF
  - Metasploit Project
entities:
  - vulnerability: clickjacking
  - technique: UI redressing
  - attack: likejacking
  - attack: nested clickjacking
  - attack: CursorJacking
  - attack: MouseJacking
  - attack: Cookiejacking
  - attack: Filejacking
  - attack: password manager attack
  - standard: X-Frame-Options
  - tool: BeEF
  - tool: Metasploit Project
claims:
  - clickjacking | is a | user interface redress attack
  - clickjacking | is an instance of | confused deputy problem
  - Jeremiah Grossman and Robert Hansen | coined the term | clickjacking
  - clickjacking | tricks a user into | clicking on something different from what the user perceives
  - classic clickjacking | may be facilitated by | other web attacks such as XSS
  - nested clickjacking | exploits a vulnerability in | the HTTP header X-Frame-Options
  - CursorJacking | was discovered in | 2010 by Eddy Bordi
kind_of_source: article
action: Developers and security professionals should understand clickjacking techniques and implement defenses like proper X-Frame-Options headers to protect users from being tricked into performing unintended actions.
source: 0bb50af22116
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

Clickjacking, also known as UI redressing, is a malicious attack that deceives users into clicking on hidden interface elements, causing them to perform unintended actions such as revealing information or granting system control. The term was coined in 2008 by researchers Jeremiah Grossman and Robert Hansen after discovering a related vulnerability in Adobe Flash Player. The attack works by overlaying a transparent layer on a legitimate webpage, making users interact with a concealed page, often for authentication or financial transactions. Several categories exist, including classic browser-based attacks, likejacking on social media, and nested attacks exploiting the X-Frame-Options header. Defensive measures are necessary as these attacks can be automated and combined with other exploits like cross-site scripting.

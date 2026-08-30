---
kind: summary
title: Fuzzing (Fuzz Testing)
identifiers: ["fuzz", "fuzzing", "ClusterFuzz", "AFL", "Shellshock", "Heartbleed", "Cyber Grand Challenge", "Mayhem", "Project Springfield", "OSS-Fuzz", "OneFuzz"]
entities: ["technique: fuzzing", "person: Barton Miller", "organization: University of Wisconsin", "tool: AFL", "vulnerability: Shellshock", "vulnerability: Heartbleed", "service: ClusterFuzz", "event: Cyber Grand Challenge"]
claims: ["fuzzing | is a | automated software testing technique", "Barton Miller | coined the term | fuzz", "early fuzzing | would now be called | black box, generational, unstructured fuzzing", "fuzzing | was used to find | Shellshock vulnerabilities", "fuzzing | could have found | Heartbleed vulnerability", "fuzzing | was used as an offense strategy in | Cyber Grand Challenge"]
kind_of_source: article
action: Security professionals and software developers should incorporate fuzzing into their testing processes, especially for programs handling untrusted input, as it is an effective technique for discovering critical security vulnerabilities.
source: cf7c4f9d325d
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

Fuzzing is an automated software testing technique that feeds a program invalid, unexpected, or random data to trigger crashes or other exceptions, revealing hidden bugs. The technique is particularly valuable for security testing of programs that process structured input from untrusted sources, as it probes edge cases that developers might miss. Its history began with a 1988 University of Wisconsin class project led by Barton Miller, who coined the term, demonstrating that random input could crash many UNIX utilities. Fuzzing has since evolved into sophisticated, tool-driven practices, playing key roles in discovering major vulnerabilities like Shellshock and forming the core of automated security services from companies like Google and Microsoft. The methodology has roots in even earlier random testing efforts from the 1950s, which were later formally validated as a cost-effective testing strategy.

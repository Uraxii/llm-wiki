---
kind: summary
title: JSON Web Token (JWT) Overview
identifiers: [JWT, "JSON Web Token", "RFC 7518", "JWA", "Base64url Encoding RFC 4648"]
entities: [standard: JSON Web Token, technique: stateless authentication, vulnerability: algorithm confusion, mechanism: SSO]
claims: [JWT | is a proposed Internet standard for creating data with optional signature and/or optional encryption whose payload holds JSON that asserts some number of claims. | , JWT tokens | are signed either using a private secret or a public/private key. | , JWT tokens | are designed to be compact, URL-safe, and usable, especially in a web-browser single-sign-on (SSO) context. | , The JWT structure | consists of a header, payload, and signature, each base64url-encoded and concatenated with periods. | , For security, JWTs | should be sent using secure mechanisms like HTTP-only cookies, not browser storage. | , A known vulnerability | involves JWT libraries incorrectly accepting tokens with `alg` set to "none".]
kind_of_source: wiki
action: When implementing JWTs for authentication, store them in HTTP-only cookies instead of browser storage to prevent client-side access, and ensure your library properly validates the signature algorithm to avoid `alg=none` attacks.

source: fa39a7c343e2
fetched: 2026-08-29
model: deepseek/deepseek-v3.2
prompt_fingerprint: 8e1e127378d2
---

JSON Web Token (JWT) is an Internet standard for creating compact, URL-safe tokens that carry JSON payloads containing claims, which are typically used to pass authenticated user identity in web and SSO contexts. A JWT is composed of three parts - a header specifying the signature algorithm, a payload containing standard and custom claims, and a signature for validation - each Base64url-encoded and concatenated with periods. For secure use in authentication, JWTs should be transmitted via HTTP-only cookies to prevent client-side JavaScript access, thereby maintaining statelessness while protecting the token. However, JWTs have known vulnerabilities, such as libraries incorrectly accepting unsigned tokens when the algorithm field is set to "none," requiring careful implementation to ensure proper validation.

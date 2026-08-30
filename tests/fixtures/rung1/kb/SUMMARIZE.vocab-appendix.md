
## Identifiers: one key, this pass only

The rule above is now replaced. This kb declares exactly one identifier
key, `dish`. Every page carries exactly one identifier, and it is the
`dish` field repeated:

    identifiers:
      - dish:creme brulee

The value must be character for character the value of the `dish` field
on the same page: lowercase, unaccented, no articles, the plainest name
a cook would use. Two recipes for the same thing must land on the same
string, because that string is the only thing that files them together.

Emit no other key. `ingredient`, `cuisine` and `course` are not
declared, and a page carrying one of them is rejected.

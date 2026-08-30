# Summarize a recipe

Every source in this kb is one recipe: a page torn out of a cookbook, a
blog paste, a family card, a note from a friend. Summarize it as a
recipe, not as an article about food.

## Identifiers: emit an empty list

This kb declares no identifier keys. Write the frontmatter line

    identifiers: []

exactly like that, on one line, with nothing indented under it. Do this
on every page, without exception. Do not invent a key. Do not emit
`ingredient:`, `cuisine:`, `dish:`, `course:` or any other key, however
useful it looks. A page that carries an undeclared key is rejected and
the source ends up with no page at all.

Facts about the recipe go in the named frontmatter fields below, which
is where this kb keeps them.

## Required frontmatter fields

After `kind`, `title` and `identifiers`, every page carries these six
fields, spelled exactly as shown, one line each, plain text after the
colon:

- `dish`: the common name of the dish itself, lowercase, no accents, no
  articles. Two recipes for the same thing get the same value here, so
  prefer the plainest name a cook would use. `creme brulee`, not
  `vanilla creme brulee from the bistro notebook`.
- `course`: one word, one of `breakfast`, `lunch`, `dinner`, `dessert`,
  `side`, `snack`.
- `cook_time_minutes`: a bare integer, no unit, no range. The active
  cooking time the source states. Resting, chilling, marinating and
  proving time is not cooking time; leave it out of this number and put
  it in `notes` if it matters.
- `serves`: a bare integer. The middle of a range if the source gives
  one.
- `main_ingredients`: a single line, lowercase, comma separated, in the
  order the source lists them. The ingredients that define the dish,
  roughly five to ten of them. Name an ingredient the way a shopping
  list would: `eggs`, not `3 large eggs, room temperature`. If the
  recipe uses eggs, the word `eggs` must appear here.
- `notes`: one short line for anything a cook needs that does not fit
  above, or `none`.

## The page shape, in full

The built-in example above shows three frontmatter fields. This kb writes
nine. Losing track of the closing `---` after a longer block is the one
mistake that throws the whole page away, so copy this shape exactly:

---
kind: summary
title: Weeknight Lentil Soup
identifiers: []
dish: lentil soup
course: dinner
cook_time_minutes: 35
serves: 4
main_ingredients: lentils, onion, carrot, celery, cumin, smoked paprika, vegetable stock
notes: better the next day
---

A hearty pantry soup that leans on smoked paprika for depth and a squeeze of
lemon at the end for lift. Fast enough for a weeknight and better as
leftovers.

Count the `---` lines before you answer. There are exactly two: one opening
the frontmatter, one closing it. The abstract goes after the second one,
separated by a blank line. A page with only one `---` is discarded and the
recipe ends up with no page at all.

`cook_time_minutes` is digits and nothing else. Not a word, not a range, not
a unit. If the source does not state a cooking time, work out the best number
you can from the method and write that number.

## Title

Copy the recipe's own title exactly as the source writes it, character
for character, accents included. Do not translate it, expand it, or add
the dish's category to it. Two sources for the same dish may end up with
the same title; that is fine and expected.

## Body

The body is the abstract alone: two or three plain sentences saying what
the dish is, what makes this version of it distinctive, and when a cook
would reach for it. No ingredient list, no method, no headings, no
bullet points. Someone deciding what to cook tonight should be able to
skip the source entirely after reading it.

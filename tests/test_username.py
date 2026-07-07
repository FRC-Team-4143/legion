"""Username generation: `last.first`, truncated to 4 chars each, plus collisions."""
from app.services.username import assign_unique_username, generate_username


def test_two_word_name():
    assert generate_username("Alexander Hamilton") == "hami.alex"


def test_three_word_name_uses_first_and_last_token():
    assert generate_username("Jane Doe Smith") == "smit.jane"


def test_single_word_name():
    assert generate_username("Madonna") == "mado"


def test_short_name_not_padded():
    assert generate_username("Al B") == "b.al"


def test_lowercases_and_strips_punctuation():
    # Punctuation is stripped from each token independently before truncation.
    assert generate_username("O'Brien Jr.") == "jr.obri"
    assert generate_username("Mary-Anne O'Neil") == "onei.mary"


def test_empty_name_falls_back():
    assert generate_username("   ") == "user"


async def test_assign_unique_username_no_collision(db):
    name = await assign_unique_username(db, "Alexander Hamilton")
    assert name == "hami.alex"


async def test_assign_unique_username_suffixes_on_collision(db, make_member):
    await make_member(name="Alexander Hamilton", username="hami.alex")
    second = await assign_unique_username(db, "Alexander Hamilton")
    assert second == "hami.alex2"

    await make_member(name="Alexander Hamilton", username="hami.alex2")
    third = await assign_unique_username(db, "Alexander Hamilton")
    assert third == "hami.alex3"


async def test_assign_unique_username_exclude_id_allows_self(db, make_member):
    member = await make_member(name="Alexander Hamilton", username="hami.alex")
    same = await assign_unique_username(db, "Alexander Hamilton", exclude_id=member.id)
    assert same == "hami.alex"

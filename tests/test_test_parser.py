from __future__ import annotations



from robber.diagnostics.test_parser import TracebackParser





def test_pytest_short_traceback_extracts_line_and_name() -> None:

    blob = """

F                                                                        [100%]

================================== FAILURES ===================================

__________________________________ test_add ___________________________________

tests/test_math.py:5: in test_add

    assert add(1, 1) == 3

E       assert 2 == 3

=========================== short test summary info ===========================

FAILED tests/test_math.py::test_add - assert 2 == 3

"""

    hit = TracebackParser().parse(blob)

    assert hit.runner == "pytest"

    assert hit.path.endswith("tests/test_math.py")

    assert hit.line == 5

    assert hit.test_name == "test_add"

    assert "assert 2 == 3" in hit.assertion

    assert hit.received == "2"

    assert hit.expected == "3"

    assert hit.frames





def test_go_test_got_want() -> None:

    blob = """

--- FAIL: TestAdd (0.00s)

    math_test.go:8: got 2 want 3

FAIL

"""

    hit = TracebackParser().parse(blob)

    assert hit.runner == "go"

    assert hit.test_name == "TestAdd"

    assert hit.path.endswith("math_test.go")

    assert hit.line == 8

    assert hit.received == "2"

    assert hit.expected == "3"





def test_cargo_panic_left_right() -> None:

    blob = """

test tests::add ... FAILED



thread 'tests::add' panicked at src/lib.rs:4:5:

assertion failed: `(left == right)`

  left: `2`

 right: `3`

"""

    hit = TracebackParser().parse(blob)

    assert hit.runner == "cargo"

    assert hit.test_name == "tests::add"

    assert hit.path.endswith("src/lib.rs")

    assert hit.line == 4

    assert hit.received == "2"

    assert hit.expected == "3"





def test_jest_expected_received() -> None:

    blob = """

FAIL  tests/math.test.js

  ● add › ones



    expect(received).toBe(expected)



    Expected: 3

    Received: 2



      4 |   expect(add(1,1)).toBe(3);

        |                    ^

      at Object.<anonymous> (tests/math.test.js:4:20)

"""

    hit = TracebackParser().parse(blob)

    assert hit.runner == "jest"

    assert hit.path.endswith("tests/math.test.js")

    assert hit.line == 4

    assert hit.expected == "3"

    assert hit.received == "2"

    assert "add" in hit.test_name


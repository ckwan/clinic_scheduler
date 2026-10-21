from app.demo import main


def test_demo_runs_and_shows_the_guardrails(capsys):
    main()
    out = capsys.readouterr().out
    assert "You're booked" in out
    assert "did not match our records" in out
    assert "1 appointment booked" in out

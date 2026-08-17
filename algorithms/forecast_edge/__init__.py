"""Forward-test of a forecaster against the market price.

Registers no algorithm and places no orders. This is measurement
infrastructure: it asks whether a forecaster is more accurate than the number
it would be betting against, which is the precondition for any strategy built
on prediction rather than structure.
"""

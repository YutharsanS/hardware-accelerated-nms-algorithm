"""Integer golden model and verification vectors for the NMS accelerator.

The modules here are the reference the RTL is checked against, bit for bit:

* :mod:`models.nms.params` -- the frozen constants, mirroring ``docs/architecture.md``.
* :mod:`models.nms.model` -- the integer NMS algorithm in both the textbook sequential
  form and the all-pairs form the hardware implements.

Nothing here uses floating point once a batch has been quantised. The hardware evaluates
the same integer predicate, so agreement is bit-exact by construction rather than by
tolerance -- which matters because the disagreements a tolerance would hide are precisely
the pairs sitting on the IoU threshold.
"""

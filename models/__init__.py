"""Python models for the NMS accelerator project.

Two packages sit here:

* :mod:`models.nms` -- the integer golden model for the NMS accelerator, the vector
  generator its VHDL testbenches read, and the CPU baseline. This is the reference the
  RTL is checked against, bit for bit.
* :mod:`models.gs` -- 3D Gaussian Splatting analysis from Phase 0, retained because it
  specifies the sorter extension described in the plan's Part D.
"""

# QuCl's UDP helper; Q2NS supplies the protocol applications.
build_lib(
  LIBNAME cosim
  SOURCE_FILES model/cosim-agent-app.cc
  HEADER_FILES model/cosim-agent-app.h
  LIBRARIES_TO_LINK ${libcore} ${libnetwork}
)

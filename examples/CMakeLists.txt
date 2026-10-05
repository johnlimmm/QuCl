foreach(example cosim-chained cosim-provisioned)
  build_lib_example(
    NAME ${example}
    SOURCE_FILES ${example}.cc
    LIBRARIES_TO_LINK ${libcosim} ${libq2ns} ${libinternet} ${libpoint-to-point} ${libtraffic-control}
  )
endforeach()

# Needs: sudo apt install libcurl4-openssl-dev nlohmann-json3-dev
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra

team_logos: team_logos.cpp
	$(CXX) $(CXXFLAGS) -o $@ $< -lcurl

clean:
	rm -f team_logos

.PHONY: clean

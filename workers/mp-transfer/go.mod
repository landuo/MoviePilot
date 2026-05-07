module github.com/landuo/MoviePilot/workers/mp-transfer

go 1.22

require (
	github.com/landuo/MoviePilot/workers/shared v0.0.0
	golang.org/x/sys v0.4.0
)

replace github.com/landuo/MoviePilot/workers/shared => ../shared

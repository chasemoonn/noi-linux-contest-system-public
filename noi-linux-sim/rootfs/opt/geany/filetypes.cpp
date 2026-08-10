[build-menu]
FT_00_LB=_Compile
FT_00_CM=g++ -O2 -std=c++14 -Wall -Wextra -c "%f"
FT_00_BD=
FT_01_LB=_Build
FT_01_CM=g++ -O2 -std=c++14 -Wall -Wextra -o "%e" "%f"
FT_01_BD=
FT_02_LB=_Check
FT_02_CM=g++ -O2 -std=c++14 -Wall -Wextra -fsyntax-only "%f"
FT_02_BD=
EX_00_LB=_Execute
EX_00_CM="./%e"
EX_01_LB=
EX_01_CM=
EX_02_LB=
EX_02_CM=


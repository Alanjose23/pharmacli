# define a base pharma cli error


class PharmaError(Exception):
    pass


# a rule name that does not exist, or options it does not accept. cli.buildrule
# catches this one by name and exits EXIT_BADRULE
class RuleError(PharmaError):
    pass


# a page the CMS API would not give up; cmdfetch catches it by name
class FetchError(PharmaError):
    pass
